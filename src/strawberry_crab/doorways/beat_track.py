"""Real-time tempo and beat-phase estimation on a mono audio stream (WIRING.md §4c).

Pure numpy, no audio libraries: aubio and librosa are awkward to install on Python 3.12
and we need only the parts a dancing crab cares about.

    tracker = BeatTracker(sample_rate=22050)
    tracker.feed(samples, now)          # float32 mono, any chunk size, wall-clock time of the chunk's end
    tracker.estimate(now) -> Tempo | None

Method. Per 1024-sample frame (512 hop, ~43 frames/s) take the log-magnitude spectrum and
its half-wave-rectified flux (energy that appeared since the last frame) in three bands:
kick (< 150 Hz), mid (to 2.5 kHz) and high. Each band is normalised by its own mean over the
window before they are added, so the kick's seven bins get the same say as the hi-hats'
three hundred; summing all bins, as the first version did, let the hats double hip hop and
the backbeat halve techno. The envelope is detrended (minus its local 0.3 s mean, rectified)
and blurred over ~3 frames, so a beat period that falls between two integer frame lags still
meets its peak (120 BPM is 21.5 frames; the old integer lags reported 60).

Tempo: autocorrelate the last eight seconds and score every candidate from 55 to 215 BPM as
ac(lag) + ½·ac(2·lag), times a log-Gaussian prior centred on 118 BPM (0.9 octave wide).
Half and double of the winner are re-scored side by side before the choice (octave folding).
A held tempo stays unless a candidate scores 15 % better twice in a row (hysteresis); the
last four seconds are scored on their own as well, so a tempo change re-locks without
waiting for the old tempo to leave the eight-second window. The reported BPM is the median
of the last three estimates since the lock.

Phase: a comb at the fractional period over the last four seconds. The kick band leads, in
linear magnitude this time (a loud kick outweighs a quiet off-beat bass note, which log
flux made equal), with the full envelope at a third of its weight behind it. The
result is blended with the grid the previous estimate predicted, and a jump of a quarter
beat or more must be measured twice before the grid moves, so next_beat does not wobble.

Alongside: `confidence` (normalised autocorrelation at the period), `steady` (the tempo has
held for two estimates and two seconds since the last lock, confidence >= 0.3, and the last
second is not silent: the widget only dances on the beat when it is set), `evenness` (how
steadily the beat repeats at 1, 2 and 4 periods; four-on-the-floor scores high),
`low_ratio` (kick weight below 150 Hz), `density` (onsets per second) and `loudness_db`.
`scripts/beat_eval.py` scores all of this on a generated test set.

Above the beat (beat_structure.py): the bar, from an accent per beat of the window (a kick
stronger than the others, a change in the spectrum's shape from one beat to the next, the low
end, the onset), voted on per position in the bar across estimates; and the section (steady,
build, drop, break) from quarter-second blocks of loudness, low end, top and onset flux over the
last minute. `pending()` says the tracker is about to change its mind (a challenger tempo, a
phase move to confirm), so the watcher can ask again sooner.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

try:
    from .beat_structure import Bars, Sections
except ImportError:      # loaded from a file by scripts/beat_eval.py --tracker, outside the package
    from strawberry_crab.doorways.beat_structure import Bars, Sections

MIN_BPM = 55.0
MAX_BPM = 215.0
PRIOR_BPM = 118.0
PRIOR_SIGMA_OCTAVES = 0.9
LOW_HZ = 150.0
HIGH_HZ = 2500.0
SUB_HZ = 100.0                   # the sections' low end: kick and bass, under where chords leak in
BAND_WEIGHTS = (1.0, 1.0, 1.0)   # kick, mid, high, each normalised by its own mean: equal say per band
HOLD_MARGIN = 1.15               # a new tempo must score this much better than the held one...
CONFIRM = 2                      # ...this many estimates in a row before it takes over
TOL = 0.04                       # "the same tempo": within 4 %
STEADY_ESTIMATES = 2
STEADY_S = 2.0
STEADY_CONFIDENCE = 0.3
PHASE_BROADBAND = 0.35           # weight of the all-band envelope against the kick in the phase comb
                                 # (more than ~0.4 and off-beat hats plus an off-beat bass win)
PHASE_WINDOW_S = 4.0
PHASE_BLEND = 0.5                # weight of the newly measured phase against the predicted one
MIN_SECONDS = 4.0
SILENT_DB = -60.0
HISTORY = 3                      # the reported BPM is the median of this many estimates since the lock
PROFILE_BANDS = 16               # the spectrum's shape, for the change from one beat to the next (the bar)
PROFILE_HZ = (60.0, 5000.0)
# How much each per-beat feature says "a bar starts here" (each z-scored over the beats of the window, and
# counted only as far as it repeats with the bar: _bar_periodic): a stronger kick; the spectrum of the two
# beats from here unlike the two before (a new chord or bass note); a hit unlike the others (an accented
# click, a crash); and a snare a little against (the backbeat is on 2 and 4). Chosen on the generated set
# (scripts/beat_eval.py's `downbeat`); the low end over the beat made it worse there (a second kick in the
# bar's second half pulled the downbeat to three) and is left out.
ACCENT_WEIGHTS = {"kick": 1.0, "change": 1.2, "distinct": 1.0, "snare": -0.3}
# The smallest spread a feature is z-scored by: shapes that differ by less than this (cosine distance) are the
# same sound, and the rest is rounding (an interval one frame longer every few beats) that z-scores into a rhythm.
ACCENT_FLOOR = {"change": 0.01, "distinct": 0.01}


@dataclass(frozen=True)
class Tempo:
    bpm: float
    period_s: float
    confidence: float     # 0..1, normalised autocorrelation at the beat period
    next_beat: float      # wall-clock (unix) time of the next beat
    evenness: float       # 0..1, mean normalised autocorrelation at 1, 2 and 4 periods
    low_ratio: float      # 0..1, share of spectral energy below LOW_HZ
    density: float        # onsets per second above the envelope's mean + std
    loudness_db: float    # RMS in dBFS over the last four seconds
    steady: bool = False  # the tempo has held for a while: dance on the beat, not just sway
    # The bar (PROTOCOL §4): beats in it, where the beat at next_beat is in it (0 = the downbeat),
    # when the next downbeat is (wall clock), and how sure that is.
    beats_per_bar: int | None = None
    beat_index: int | None = None
    next_downbeat: float | None = None
    downbeat_confidence: float | None = None
    # The section: steady, build, drop or break, how sure, and since when (wall clock).
    section: str | None = None
    section_confidence: float | None = None
    section_since: float | None = None

    def to_dict(self) -> dict:
        """The fields that are set, floats rounded (wall-clock times to the millisecond)."""
        out = {}
        for k, v in asdict(self).items():
            if v is None:
                continue
            if isinstance(v, float):
                v = round(v, 3) if k in ("next_beat", "next_downbeat", "section_since") else round(v, 4)
            out[k] = v
        return out


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= TOL * b


class BeatTracker:
    def __init__(self, sample_rate: int = 22050, n_fft: int = 1024, hop: int = 512, window_s: float = 8.0) -> None:
        self.sr = sample_rate
        self.n_fft = n_fft
        self.hop = hop
        self.fps = sample_rate / hop
        self.window = np.hanning(n_fft).astype(np.float32)
        self.capacity = int(window_s * self.fps)
        # Flux per frame: log-magnitude rise in the kick, mid and high bands (the tempo), and the
        # linear-magnitude rise in the kick band (the phase: loudness matters there, see _phase).
        self.bands = np.zeros((4, self.capacity), dtype=np.float32)
        self.low = np.zeros(self.capacity, dtype=np.float32)
        self.rms = np.zeros(self.capacity, dtype=np.float32)
        self.filled = 0
        self.buffer = np.zeros(0, dtype=np.float32)
        self.prev_log: np.ndarray | None = None
        self.prev_low: np.ndarray | None = None
        self.last_frame_time = 0.0
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
        self.low_bins = freqs < LOW_HZ
        self.high_bins = freqs >= HIGH_HZ
        self.sub_bins = freqs < SUB_HZ
        edges = np.geomspace(*PROFILE_HZ, PROFILE_BANDS + 1)
        self.profile_matrix = np.stack([(freqs >= a) & (freqs < b) for a, b in zip(edges[:-1], edges[1:])],
                                       axis=1).astype(np.float32)
        self.profile_matrix /= np.maximum(self.profile_matrix.sum(axis=0, keepdims=True), 1.0)
        self.profile = np.zeros((PROFILE_BANDS, self.capacity), dtype=np.float32)
        self.bars = Bars()
        self.sections = Sections(self.fps)
        self.band_slices = [slice(1, int(np.searchsorted(freqs, LOW_HZ))),
                            slice(int(np.searchsorted(freqs, LOW_HZ)), int(np.searchsorted(freqs, HIGH_HZ))),
                            slice(int(np.searchsorted(freqs, HIGH_HZ)), len(freqs))]
        bpms = np.geomspace(MIN_BPM, MAX_BPM, 320)
        self.cand_bpm = bpms
        self.cand_lag = 60.0 * self.fps / bpms
        self.cand_prior = np.exp(-0.5 * (np.log2(bpms / PRIOR_BPM) / PRIOR_SIGMA_OCTAVES) ** 2)
        blur = np.array([0.25, 0.75, 1.0, 0.75, 0.25])
        self.blur = blur / blur.sum()
        self._reset_lock()

    def _reset_lock(self) -> None:
        self.held: float | None = None       # the tempo we are locked to
        self.held_since = 0.0
        self.held_count = 0
        self.history: list[float] = []        # raw estimates since the lock, for the median
        self.challenger: float | None = None
        self.challenger_votes = 0
        self.beat_ref: float | None = None    # wall-clock time of a beat, for phase continuity
        self.phase_jump: float | None = None  # an unconfirmed phase move, in periods
        if hasattr(self, "bars"):
            self.bars.reset()                 # a new tempo or grid: the bar positions start again

    @property
    def seconds(self) -> float:
        return min(self.filled, self.capacity) / self.fps

    def reset(self) -> None:
        self.filled = 0
        self.buffer = np.zeros(0, dtype=np.float32)
        self.prev_log = None
        self.prev_low = None
        self.sections.reset()
        self._reset_lock()

    def pending(self) -> bool:
        """The tracker is about to change its mind: a tempo that must win once more, or a phase move to
        confirm. The next estimate settles it, so the watcher asks sooner (beat_watch.Watcher)."""
        return self.challenger is not None or self.phase_jump is not None

    def feed(self, samples: np.ndarray, now: float) -> None:
        """`now` is the wall-clock time at which the last sample in `samples` was heard."""
        self.buffer = np.concatenate([self.buffer, samples.astype(np.float32, copy=False)])
        total = len(self.buffer)
        if total < self.n_fft:
            return
        count = (total - self.n_fft) // self.hop + 1
        frames = np.lib.stride_tricks.sliding_window_view(self.buffer, self.n_fft)[::self.hop][:count]
        # Each frame's last sample, in wall-clock time: `now` minus what is still unread after it.
        ends = now - (total - (np.arange(count) * self.hop + self.n_fft)) / self.sr
        self._push(frames, ends)
        consumed = count * self.hop
        self.last_frame_time = float(ends[-1])
        self.buffer = self.buffer[consumed:]

    def _push(self, frames: np.ndarray, ends: np.ndarray) -> None:
        """Features for a batch of frames (one FFT call per feed, not one per frame)."""
        spectrum = np.abs(np.fft.rfft(frames * self.window, axis=1)).astype(np.float32)
        log_spec = np.log1p(20.0 * spectrum)
        low = spectrum[:, self.band_slices[0]]
        prev_log = log_spec[:1] if self.prev_log is None else self.prev_log[None, :]
        prev_low = low[:1] if self.prev_low is None else self.prev_low[None, :]
        rise = np.maximum(np.diff(log_spec, axis=0, prepend=prev_log), 0.0)
        feats = np.empty((4, len(frames)), dtype=np.float32)
        for b, sl in enumerate(self.band_slices):
            feats[b] = rise[:, sl].mean(axis=1)
        feats[3] = np.maximum(np.diff(low, axis=0, prepend=prev_low), 0.0).sum(axis=1)
        self.prev_log = log_spec[-1]
        self.prev_low = low[-1]
        power = spectrum * spectrum
        total = power.sum(axis=1)
        lowpow = power[:, self.low_bins].sum(axis=1)
        low_ratio = lowpow / (total + 1e-9)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        idx = (self.filled + np.arange(len(frames))) % self.capacity
        self.bands[:, idx] = feats
        self.low[idx] = low_ratio
        self.rms[idx] = rms
        self.profile[:, idx] = (power @ self.profile_matrix).T
        self.filled += len(frames)
        # The section works on power shares of the frame, so the player's volume knob moves all of them alike.
        scale = float(self.n_fft) ** 2
        self.sections.feed(ends, rms * rms, power[:, self.sub_bins].sum(axis=1) / scale,
                           power[:, self.high_bins].sum(axis=1) / scale,
                           feats[:3].sum(axis=0))

    def _recent(self, ring: np.ndarray, frames: int) -> np.ndarray:
        n = min(self.filled, self.capacity, frames)
        end = self.filled % self.capacity
        idx = (np.arange(end - n, end)) % self.capacity
        return ring[..., idx]

    def _envelope(self, frames: int) -> tuple[np.ndarray, np.ndarray]:
        """(onset envelope, linear kick-band envelope), both detrended, rectified and blurred."""
        bands = self._recent(self.bands, frames).astype(np.float64)
        bands = bands / (bands.mean(axis=1, keepdims=True) + 1e-6)
        env = np.tensordot(np.asarray(BAND_WEIGHTS), bands[:3], axes=1)
        width = max(3, int(round(0.3 * self.fps)) | 1)
        out = []
        for x in (env, bands[3]):
            local = np.convolve(x, np.ones(width) / width, mode="same")
            x = np.maximum(x - local, 0.0)
            out.append(np.convolve(x, self.blur, mode="same"))
        return out[0], out[1]

    def _tempo_scores(self, env: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        """Normalised autocorrelation and the prior-weighted score per candidate BPM."""
        x = env - env.mean()
        n = len(x)
        spec = np.fft.rfft(x, 2 * n)
        ac = np.fft.irfft(spec * np.conj(spec))[:n]
        if ac[0] <= 1e-9:
            return None
        ac = ac / ac[0]
        lags = np.arange(n)
        at = np.interp(self.cand_lag, lags, ac, right=0.0)
        at2 = np.interp(2.0 * self.cand_lag, lags, ac, right=0.0)
        raw = np.clip(at, 0.0, None) + 0.5 * np.clip(at2, 0.0, None)
        return ac, raw * self.cand_prior

    def _best(self, scores: np.ndarray) -> float:
        """Best candidate, then octave folding: half and double of it re-scored against it."""
        i = int(np.argmax(scores))
        best = float(self.cand_bpm[i])
        best_score = float(scores[i])
        for k in (0.5, 2.0):
            alt = best * k
            if MIN_BPM <= alt <= MAX_BPM:
                s = self._score_near(scores, alt)
                if s[1] > best_score:
                    best, best_score = s
        return best

    def _score_near(self, scores: np.ndarray, bpm: float, tol: float = TOL) -> tuple[float, float]:
        """Local maximum of `scores` within `tol` of `bpm`: (bpm, score)."""
        mask = np.abs(self.cand_bpm - bpm) <= tol * bpm
        if not mask.any():
            return bpm, 0.0
        idx = np.flatnonzero(mask)
        j = idx[int(np.argmax(scores[idx]))]
        return float(self.cand_bpm[j]), float(scores[j])

    def _choose(self, scores: np.ndarray, recent_scores: np.ndarray | None, now: float) -> float:
        """Hysteresis between the held tempo and whatever the window now prefers."""
        best = self._best(scores)
        if self.held is None:
            self._lock(best, now)
            return best
        held_bpm, held_score = self._score_near(scores, self.held)
        candidate, convincing = best, self._score_near(scores, best)[1] > HOLD_MARGIN * held_score
        if recent_scores is not None:
            recent = self._best(recent_scores)
            if not _same(recent, self.held) and \
                    self._score_near(recent_scores, recent)[1] > HOLD_MARGIN * self._score_near(recent_scores, self.held)[1]:
                # The last four seconds alone prefer another tempo: a change the long window
                # still hides. Their vote counts, and the confirmation below still applies.
                candidate, convincing = recent, True
        if _same(candidate, self.held) or not convincing:
            self.challenger, self.challenger_votes = None, 0
            return held_bpm
        if self.challenger is not None and _same(candidate, self.challenger):
            self.challenger_votes += 1
        else:
            self.challenger, self.challenger_votes = candidate, 1
        if self.challenger_votes >= CONFIRM:
            self._lock(candidate, now)
            return candidate
        return held_bpm

    def _lock(self, bpm: float, now: float) -> None:
        self.held = bpm
        self.held_since = now
        self.held_count = 0
        self.history = []
        self.challenger, self.challenger_votes = None, 0
        self.beat_ref = None
        self.phase_jump = None

    def estimate(self, now: float) -> Tempo | None:
        if self.seconds < MIN_SECONDS:
            return None
        env, kicks = self._envelope(self.capacity)
        result = self._tempo_scores(env)
        if result is None:
            return None
        ac, scores = result
        recent_scores = None
        four = int(4.0 * self.fps)
        if len(env) > four + 8:
            r = self._tempo_scores(env[-four:])
            recent_scores = r[1] if r is not None else None
        raw = self._choose(scores, recent_scores, now)
        # Refine the period on the autocorrelation peak nearest the chosen tempo.
        lag = self._refine_peak(ac, int(round(60.0 * self.fps / raw)))
        if not (60.0 * self.fps / MAX_BPM * 0.9 <= lag <= 60.0 * self.fps / MIN_BPM * 1.1) or not _same(60.0 * self.fps / lag, raw):
            lag = 60.0 * self.fps / raw
        raw_bpm = 60.0 * self.fps / lag
        self.history.append(raw_bpm)
        self.history = self.history[-HISTORY:]
        self.held_count += 1
        bpm = float(np.median(self.history))
        self.held = bpm
        period = 60.0 / bpm
        lag = period * self.fps
        confidence = float(np.clip(self._interp(ac, lag), 0.0, 1.0))
        n = len(env)
        multiples = [self._interp(ac, lag * k) for k in (1.0, 2.0, 4.0) if lag * k < n - 1]
        evenness = float(np.clip(np.mean(multiples), 0.0, 1.0)) if multiples else confidence
        # Steady: the same tempo for a while, a clear peak, and still sounding right now (the
        # window remembers the song for eight seconds after it stops).
        audible = 20.0 * math.log10(max(float(self._recent(self.rms, int(self.fps)).mean()), 1e-6)) > SILENT_DB
        steady = (self.held_count >= STEADY_ESTIMATES and now - self.held_since >= STEADY_S
                  and confidence >= STEADY_CONFIDENCE and audible)

        previous_ref = self.beat_ref
        next_beat = self._phase(env, kicks, period, now)
        if previous_ref is None or self.beat_ref is None:
            self.bars.reset()
        if self.beat_ref is not None:
            accents, three = self._accents(env, kicks, period)
            self.bars.update(accents, self.beat_ref, period, three)
        beats_per_bar, beat_index, next_downbeat, downbeat_confidence = self.bars.position(next_beat, period)
        section_since = self.sections.since if self.sections.since is not None else now
        if self.beat_ref is not None and confidence >= STEADY_CONFIDENCE:
            # Sections start on a beat; the blocks only know the quarter second.
            section_since = self.beat_ref + round((section_since - self.beat_ref) / period) * period

        m = min(four, n)
        recent = env[-m:]
        threshold = recent.mean() + recent.std()
        density = float((recent > threshold).sum()) / (m / self.fps)
        low_ratio = float(self._recent(self.low, m).mean())
        rms = float(self._recent(self.rms, m).mean())
        loudness_db = 20.0 * math.log10(max(rms, 1e-6))
        return Tempo(bpm, period, confidence, next_beat, evenness, low_ratio, density, loudness_db, steady,
                     beats_per_bar, beat_index, next_downbeat, downbeat_confidence * min(1.0, confidence / 0.5),
                     self.sections.name, self.sections.confidence, section_since)

    def _accents(self, env: np.ndarray, kicks: np.ndarray, period: float) -> tuple[np.ndarray, float]:
        """Per beat of the window, newest first (index j: j beats before beat_ref): how much a bar might start
        there; and how much better the clearest feature fits a bar of three than of four (Bars' meter). Each
        feature is z-scored over the beats, so one that is the same on every beat (the kick of
        four-on-the-floor) says nothing, weighted by how well it repeats with a bar (_bar_fit), and they are
        summed with ACCENT_WEIGHTS."""
        m = len(env)
        lag = period * self.fps
        centre = self.last_frame_time - 0.5 * self.n_fft / self.sr
        f0 = (m - 1) - (centre - self.beat_ref) * self.fps
        profile = self._recent(self.profile, m).astype(np.float64)
        mid = self._recent(self.bands[1], m).astype(np.float64)
        mid = np.convolve(mid / (mid.mean() + 1e-9), self.blur, mode="same")
        def shape(frames: slice) -> np.ndarray:
            """The spectrum's shape over these frames: log of the mean power (so a click counts), unit length."""
            s = np.log(profile[:, frames].mean(axis=1) + 1e-6)
            s -= s.mean()
            return s / (float(np.linalg.norm(s)) + 1e-9)

        # Each beat with a whole beat of audio after it, by j: its frames, the shape of its beat and of its hit.
        # From a frame before the beat: a frame holds the 46 ms around its centre, so the beat is in it.
        beats = {}
        j = 0
        while (f := f0 - j * lag) >= 0:
            start, end = int(round(f)) - 1, int(round(f + lag)) - 1
            if 0 <= start and end <= m:
                beats[j] = (start, end, shape(slice(start, end)), shape(slice(start, start + 3)))
            j += 1
        if len(beats) < 4:
            return np.zeros(0), 0.0
        hits = np.mean([b[3] for b in beats.values()], axis=0)
        rows = []
        for j, (start, end, _, hit) in beats.items():
            # Two beats from here against the two before (j - 1 is the newer neighbour, j + 1, j + 2 the older).
            if not all(k in beats for k in (j - 1, j + 1, j + 2)):
                continue
            after = beats[j][2] + beats[j - 1][2]
            before = beats[j + 1][2] + beats[j + 2][2]
            change = 1.0 - float(after @ before) / (float(np.linalg.norm(after) * np.linalg.norm(before)) + 1e-9)
            around = slice(max(0, start - 1), start + 4)    # the beat's frame (start + 1) and two either side
            distinct = 1.0 - float(hit @ hits) / (float(np.linalg.norm(hits)) + 1e-9)
            # The sum over five frames, not the peak: a peak read at a frame boundary that drifts against the
            # beat (a hop is 23 ms) rises and falls every few beats, which a bar of three would then fit.
            rows.append((j, kicks[around].sum(), change, distinct, mid[around].sum()))
        if len(rows) < 4:
            return np.zeros(0), 0.0
        data = np.array(rows)
        out = np.full(int(data[:, 0].max()) + 1, np.nan)    # NaN: a beat without the beats around it
        total = np.zeros(len(data))
        js = data[:, 0].astype(int)
        three = 0.0
        for col, name in ((1, "kick"), (2, "change"), (3, "distinct"), (4, "snare")):
            x = data[:, col]
            spread = max(float(x.std()), 0.1 * abs(float(x.mean())), ACCENT_FLOOR.get(name, 1e-6))
            z = (x - x.mean()) / spread
            fit = self._bar_fit(z, js)
            total += ACCENT_WEIGHTS[name] * max(fit.values()) * z
            # Only as far as the feature varies at all (a spread at its floor is no rhythm, however regular).
            three = max(three, float(x.std()) / spread * (fit[3] - fit[4]))
        out[js] = total
        return out, three

    @staticmethod
    def _bar_fit(z: np.ndarray, js: np.ndarray) -> dict[int, float]:
        """Per bar length (3, 4 beats), 0..1: how much of a feature's variation repeats with that bar, above
        what noise would show with this few beats. A feature that only varies at random (the kick of
        four-on-the-floor, the mid band of a click track) adds nothing to the accents then, instead of
        drowning the one that counts; and one that fits three beats far better than four says waltz."""
        total = float((z * z).sum())
        fit = {3: 0.0, 4: 0.0}
        if total <= 1e-9:
            return fit
        for b in fit:
            groups = [z[js % b == r] for r in range(b)]
            if any(len(g) == 0 for g in groups):
                continue
            between = sum(len(g) * float(g.mean()) ** 2 for g in groups) / total
            chance = (b - 1) / max(len(z) - 1, b)
            fit[b] = float(np.clip((between - chance) / (1.0 - chance), 0.0, 1.0))
        return fit

    def _phase(self, env: np.ndarray, kicks: np.ndarray, period: float, now: float) -> float:
        """Wall-clock time of the next beat. Kick-band onsets lead: hats and snares sit on the
        off-beats and are brighter, so the broadband envelope alone would lock half a beat late."""
        m = min(int(PHASE_WINDOW_S * self.fps), len(env))
        grid_env = kicks[-m:] / (kicks[-m:].max() + 1e-9) + PHASE_BROADBAND * env[-m:] / (env[-m:].max() + 1e-9)
        lag = period * self.fps
        steps = int(math.floor((m - 1) / lag)) + 1
        k = np.arange(steps) * lag
        offsets = np.arange(int(math.ceil(lag)))
        pos = np.rint((m - 1 - offsets[:, None]) - k[None, :]).astype(int)
        valid = pos >= 0
        vals = np.where(valid, grid_env[np.clip(pos, 0, m - 1)], 0.0)
        score = vals.sum(axis=1) / np.maximum(valid.sum(axis=1), 1)
        best = int(np.argmax(score))
        # Sub-frame refinement around the best offset (circular in the period).
        a, b, c = score[(best - 1) % len(score)], score[best], score[(best + 1) % len(score)]
        denom = a - 2 * b + c
        frac = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        # A frame's flux belongs to the middle of its window, not to its last sample.
        centre = self.last_frame_time - 0.5 * self.n_fft / self.sr
        last_beat = centre - (best + float(np.clip(frac, -0.5, 0.5))) / self.fps
        if self.beat_ref is not None:
            # Blend with where the previous estimate said the beats would be.
            d = (last_beat - self.beat_ref) / period
            d -= round(d)
            if abs(d) < 0.25:
                last_beat -= (1.0 - PHASE_BLEND) * d * period
                self.phase_jump = None
            elif self.phase_jump is not None and abs((d - self.phase_jump) - round(d - self.phase_jump)) < 0.15:
                self.phase_jump = None   # the comb said the same thing twice: the grid really moved
                self.bars.reset()
            else:
                # One comb disagreeing by a quarter beat or more is usually a fill or a
                # syncopated bar; keep the old grid until the next estimate agrees.
                self.phase_jump = d
                last_beat -= d * period
        self.beat_ref = last_beat
        if now > last_beat:
            last_beat += period * math.floor((now - last_beat) / period)
        return last_beat + period

    @staticmethod
    def _refine_peak(ac: np.ndarray, i: int) -> float:
        """Parabolic interpolation around the local maximum nearest integer lag i."""
        if i <= 0 or i >= len(ac) - 1:
            return float(i)
        for _ in range(3):   # climb to the local peak
            if i + 1 < len(ac) - 1 and ac[i + 1] > ac[i]:
                i += 1
            elif i - 1 > 0 and ac[i - 1] > ac[i]:
                i -= 1
            else:
                break
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        denom = a - 2 * b + c
        if abs(denom) < 1e-12:
            return float(i)
        return float(i + 0.5 * (a - c) / denom)

    @staticmethod
    def _interp(ac: np.ndarray, x: float) -> float:
        i = int(math.floor(x))
        if i < 0 or i + 1 >= len(ac):
            return 0.0
        t = x - i
        return float(ac[i] * (1 - t) + ac[i + 1] * t)
