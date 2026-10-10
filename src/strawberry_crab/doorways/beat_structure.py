"""The music's structure above the beat: where the bar starts, and which section it is in (WIRING.md §4c).

Two small state machines that `beat_track.BeatTracker` feeds. Pure numpy; no audio of their own.

`Bars`: the downbeat. Each estimate hands it an accent per beat of the eight-second window (how much a
bar might start there: a kick that is stronger than the others, a change of harmony or bass note, more low
end, a crash) and it keeps a vote per position in the bar, for 4 and for 3 beats a bar, carried from one
estimate to the next on the beat grid and decaying. The best position is the downbeat; its lead over the
next best is the confidence. Three beats a bar only when they clearly fit better than four, twice in a row.

`Sections`: steady, build, drop or break, from the loudness, the low end (< 150 Hz), the top (> 2.5 kHz)
and the onset flux in quarter-second blocks over the last minute:

    break   the low end stays well under what this track usually has (its 80th percentile over a minute)
    build   rising onset density, loudness or brightness over the last six seconds, with the low end
            filtered away (under the reference, or falling against the rest: a high-pass sweep)
    drop    after a build or a break, the low end and the loudness come back at once (within half a second)
    steady  anything else, and a drop after eight seconds

A section holds at least DWELL_S before another replaces it, except a drop, which is the point of a build.
`epoch` counts the changes, so the watcher can post one as it happens. The thresholds are heuristics,
tuned on generated builds and drops (tests/test_beat_structure.py), not on records.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

SECTIONS = ("steady", "build", "drop", "break")

# --- Sections

BLOCK_S = 0.25
HISTORY_S = 64.0          # the reference ("what this track usually has") comes from this much
REF_PERCENTILE = 80.0
MIN_HISTORY_S = 8.0       # before this, everything is steady
SHORT_S = 2.0             # "now": the level a break or a return is judged on
TREND_S = 6.0             # a build's rise is measured over this
DWELL_S = 2.0             # a section holds this long before another (not a drop) replaces it
STALL_S = 4.0             # a build that has not risen for this long is over (a break, or steady)
DROP_HOLD_S = 8.0         # a drop is reported this long, then steady
DROP_FROM_S = 3.0         # a drop counts within this long after a build or a break ended
BREAK_DIP_DB = 8.0        # low end this far under the reference over SHORT_S: a break
BACK_DB = 4.0             # within this of the reference: the low end is back
FILTERED_DB = 4.0         # a build's low end is at least this far under the reference...
HIGHPASS_DB_S = -0.5      # ...or falling against the loudness this fast (dB/s): a high-pass sweep
RISE_FLUX_S = 0.06        # a build rises: onset flux (natural log per s),
RISE_LOUD_DB_S = 0.75     # loudness (dB/s)
RISE_HIGH_DB_S = 0.75     # or the top against the loudness (dB/s)
RISE_FIT = 0.4            # and the rise is a trend, not one hit (r² of the line)
DROP_JUMP_DB = 9.0        # the low end rose this much from the 2 s before to the last 0.5 s
DROP_QUIETER_DB = 3.0     # while the loudness fell no more than this (a riser can be as loud as the drop)
DROP_NEAR_DB = 6.0        # and the low end is within this of the reference
SILENT_DB = -60.0


def _db(power: float) -> float:
    return 10.0 * math.log10(max(power, 1e-12))


def _slope(t: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Least-squares slope of y over t, and its r²."""
    t = t - t.mean()
    var = float((t * t).sum())
    if var <= 0:
        return 0.0, 0.0
    slope = float((t * (y - y.mean())).sum()) / var
    total = float(((y - y.mean()) ** 2).sum())
    fit = 1.0 - float(((y - y.mean() - slope * t) ** 2).sum()) / total if total > 1e-12 else 0.0
    return slope, max(0.0, fit)


class Sections:
    def __init__(self, fps: float) -> None:
        self.block_frames = max(1, int(round(BLOCK_S * fps)))
        self.blocks: deque[tuple[float, float, float, float, float]] = deque(maxlen=int(HISTORY_S / BLOCK_S))
        self.reset()

    def reset(self) -> None:
        self.blocks.clear()
        self.pending = np.zeros(4)      # power, low power, high power, flux summed over the frames so far
        self.pending_n = 0
        self.name = "steady"
        self.confidence = 0.0
        self.since: float | None = None
        self.epoch = 0                  # how many changes so far
        self.left: dict[str, float] = {}   # when each section last ended (a drop follows a recent build)
        self.rising_at = -1e9              # when a build's rise was last measured

    def feed(self, times: np.ndarray, power: np.ndarray, low: np.ndarray, high: np.ndarray, flux: np.ndarray) -> None:
        """One value per frame: its end time (wall clock), mean power, power below and above the bands' edges,
        and onset flux. Quarter-second blocks; the state is judged at the end of each."""
        for i in range(len(times)):
            self.pending += (power[i], low[i], high[i], flux[i])
            self.pending_n += 1
            if self.pending_n >= self.block_frames:
                p, lo, hi, fl = self.pending / self.pending_n
                self.blocks.append((float(times[i]), _db(p), _db(lo), _db(hi), math.log(max(fl, 1e-9))))
                self.pending[:] = 0.0
                self.pending_n = 0
                self._judge(float(times[i]))

    def _set(self, name: str, confidence: float, since: float, now: float) -> None:
        if name != self.name:
            self.left[self.name] = now
            self.name = name
            self.since = since
            self.epoch += 1
        self.confidence = float(np.clip(confidence, 0.0, 1.0))

    def _judge(self, now: float) -> None:
        data = np.array(self.blocks)
        sounding = data[data[:, 1] > SILENT_DB]
        if self.since is None:
            self.since = now
        if len(sounding) * BLOCK_S < MIN_HISTORY_S:
            self.confidence = 0.0
            return
        if data[-1, 1] <= SILENT_DB:
            return      # a pause (before a drop, say) changes nothing
        t, loud, low, high, flux = sounding.T
        ref_low = float(np.percentile(low, REF_PERCENTILE))
        held = now - (self.since if self.since is not None else now)
        # The last half second, and the two seconds of sound before it (a pause between them does not count).
        last = t > now - 0.5
        before = np.zeros(len(t), dtype=bool)
        before[max(0, len(t) - int(last.sum()) - int(SHORT_S / BLOCK_S)):len(t) - int(last.sum())] = True

        # A drop: the low end and the loudness come back at once, after a build or a break.
        after_build = self.name in ("build", "break") or any(
            now - self.left.get(name, -1e9) <= DROP_FROM_S for name in ("build", "break"))
        if after_build and last.sum() >= 1 and before.sum() >= 4 and self.name != "drop":
            jump = float(low[last].mean() - low[before].mean())
            louder = float(loud[last].mean() - loud[before].mean())
            near = ref_low - float(low[last].mean())
            if jump >= DROP_JUMP_DB and louder >= -DROP_QUIETER_DB and near <= DROP_NEAR_DB:
                rise = np.flatnonzero(last | before)
                threshold = low[before].mean() + jump / 2
                first = next((i for i in rise if t[i] > now - 1.0 and low[i] >= threshold), int(rise[-1]))
                self._set("drop", min(1.0, 0.5 + (jump - DROP_JUMP_DB) / 12.0), float(t[first]) - BLOCK_S, now)
                return
        if self.name == "drop" and held < DROP_HOLD_S:
            return      # a drop is held: it is what a body waits for
        short = t > now - SHORT_S
        if short.sum() * BLOCK_S < 0.75 * SHORT_S:
            return      # just after a pause: not enough of "now" to judge a level on
        low_dip = ref_low - float(low[short].mean())

        # A build: something rises over the last six seconds while the low end is filtered away.
        trend = t > now - TREND_S
        build = 0.0
        build_since = now
        if trend.sum() * BLOCK_S >= TREND_S * 0.8:
            tt = t[trend]
            rises = []
            for series, need in ((flux[trend], RISE_FLUX_S), (loud[trend], RISE_LOUD_DB_S),
                                 (high[trend] - loud[trend], RISE_HIGH_DB_S)):
                slope, fit = _slope(tt, series)
                if slope >= need and fit >= RISE_FIT:
                    rises.append((min(1.0, slope / (2 * need)) * fit, series))
            low_slope, _ = _slope(tt, low[trend] - loud[trend])
            filtered = low_dip >= FILTERED_DB or low_slope <= HIGHPASS_DB_S
            if rises and filtered:
                strength, series = max(rises, key=lambda r: r[0])
                build = 0.4 + 0.6 * strength
                self.rising_at = now
                # It began where the steepest rise first got a sixth of the way up (smoothed over a second).
                smooth = np.convolve(series, np.ones(4) / 4, mode="valid")
                up = np.flatnonzero(smooth >= smooth[0] + (smooth.max() - smooth[0]) / 6)
                build_since = float(tt[min(len(tt) - 1, int(up[0]) + 3)]) - BLOCK_S if len(up) else float(tt[0])
        brk = 0.0
        if low_dip >= BREAK_DIP_DB:
            brk = min(1.0, 0.5 + (low_dip - BREAK_DIP_DB) / 12.0)

        if held < DWELL_S:
            # Too soon to move on; the confidence follows what is measured now.
            current = {"build": build, "break": brk}.get(self.name, 0.0)
            if current > 0:
                self.confidence = current
            return
        if build > 0:
            self._set("build", build, build_since, now)
        elif self.name == "build" and now - self.rising_at < STALL_S and low_dip >= BACK_DB:
            pass        # the rise paused (a held riser, the last bar before the drop): still a build
        elif brk > 0:
            # It began where the low end first went under the reference by half a break's dip.
            under = low < ref_low - BREAK_DIP_DB / 2
            i = len(t) - 1
            while i > 0 and under[i - 1] and t[i - 1] > now - HISTORY_S:
                i -= 1
            self._set("break", brk, float(t[i]) - BLOCK_S, now)
        elif self.name == "break" and low_dip >= BACK_DB:
            pass        # on its way back, not back yet
        else:
            history = len(sounding) * BLOCK_S
            self._set("steady", min(1.0, history / (2 * MIN_HISTORY_S)), now, now)


# --- Bars

METERS = (4, 3)
BAR_DECAY = 0.7           # each estimate's evidence against what the earlier ones said
# The meter: how much better the clearest accent feature fits a bar of three than of four (0..1, from the
# tracker), averaged over estimates (METER_DECAY). Three beats a bar above METER_THREE, four again below
# METER_FOUR. A waltz scores near 1; four-four, and features that only vary at random, near 0.
METER_DECAY = 0.7
METER_THREE = 0.5
METER_FOUR = 0.2


class Bars:
    """Votes per position in the bar, carried on the beat grid from one estimate to the next."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.votes = {b: np.zeros(b) for b in METERS}
        self.anchor: float | None = None     # the beat the votes' index 0 is on (wall clock)
        self.meter = 4
        self.meter_score = 0.0
        self.updates = 0

    def update(self, accents: np.ndarray, last_beat: float, period: float, three: float = 0.0) -> None:
        """`accents[j]`: how much a bar might start j beats before `last_beat` (z-scored per feature, summed;
        NaN where it is not known). `three`: how much better the music fits a bar of three than of four."""
        if self.anchor is not None:
            shift = int(round((last_beat - self.anchor) / period))
            for b in METERS:
                self.votes[b] = np.roll(self.votes[b], -shift)
        self.anchor = last_beat
        known = np.flatnonzero(~np.isnan(accents))
        if len(known) < max(METERS) + 2:
            return
        for b in METERS:
            evidence = np.array([accents[known[(-known) % b == r]].mean() for r in range(b)])
            if np.isnan(evidence).any():
                return
            self.votes[b] = BAR_DECAY * self.votes[b] + (evidence - evidence.mean())
        self.updates += 1
        self.meter_score = METER_DECAY * self.meter_score + (1 - METER_DECAY) * three
        if self.meter_score > METER_THREE:
            self.meter = 3
        elif self.meter_score < METER_FOUR:
            self.meter = 4

    def position(self, next_beat: float, period: float) -> tuple[int, int, float, float]:
        """(beats per bar, the bar position of the beat at `next_beat` (0 = the downbeat), the next downbeat's
        time, confidence 0..1)."""
        b = self.meter
        votes = self.votes[b]
        if self.anchor is None or self.updates == 0 or not np.any(votes):
            return b, 0, next_beat, 0.0
        down = int(np.argmax(votes))
        ahead = int(round((next_beat - self.anchor) / period))
        index = (ahead - down) % b
        ordered = np.sort(votes)
        spread = float(ordered[-1] - ordered[0])
        margin = float(ordered[-1] - ordered[-2]) / spread if spread > 1e-9 else 0.0
        strength = min(1.0, spread / 1.5)          # how much evidence there is at all
        warm = min(1.0, self.updates / 2.0)
        return b, index, next_beat + ((b - index) % b) * period, float(np.clip(margin * strength * warm, 0.0, 1.0))
