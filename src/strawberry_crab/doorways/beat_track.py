"""Real-time tempo and beat-phase estimation on a mono audio stream (WIRING.md §4c).

Pure numpy, no audio libraries: aubio and librosa are awkward to install on Python 3.12
and we need only the parts a dancing crab cares about.

    tracker = BeatTracker(sample_rate=22050)
    tracker.feed(samples, now)          # float32 mono, any chunk size, wall-clock time of the chunk's end
    tracker.estimate(now) -> Tempo | None

Method: per 1024-sample frame (512 hop, ~43 frames/s) take the log-magnitude spectrum and the
half-wave-rectified spectral flux (energy that appeared since the last frame): the onset
envelope. Autocorrelate the last eight seconds of it; the lag with the strongest peak in
60–190 BPM, nudged by a mild prior around 120 BPM, is the beat period. A comb filter over the
last four seconds finds the phase, so `next_beat` is a wall-clock time the widget can stomp on.
Alongside: `evenness` (how steadily the beat repeats at 1, 2 and 4 periods; four-on-the-floor
scores high), `low_ratio` (kick weight below 150 Hz), `density` (onsets per second) and
`loudness_db`. Those four plus the tempo pick her dance style.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

MIN_BPM = 60.0
MAX_BPM = 190.0
PRIOR_BPM = 120.0
PRIOR_SIGMA_OCTAVES = 0.7
CONTINUITY_BONUS = 0.6   # score multiplier (1 + bonus) for candidates within ~8% of the last confident tempo
LOW_HZ = 150.0


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

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}


class BeatTracker:
    def __init__(self, sample_rate: int = 22050, n_fft: int = 1024, hop: int = 512, window_s: float = 8.0) -> None:
        self.sr = sample_rate
        self.n_fft = n_fft
        self.hop = hop
        self.fps = sample_rate / hop
        self.window = np.hanning(n_fft).astype(np.float32)
        self.capacity = int(window_s * self.fps)
        self.onset = np.zeros(self.capacity, dtype=np.float32)
        self.kick = np.zeros(self.capacity, dtype=np.float32)   # onset flux in the kick band only
        self.low = np.zeros(self.capacity, dtype=np.float32)
        self.rms = np.zeros(self.capacity, dtype=np.float32)
        self.filled = 0
        self.buffer = np.zeros(0, dtype=np.float32)
        self.prev_log: np.ndarray | None = None
        self.last_frame_time = 0.0
        self.last_bpm: float | None = None   # continuity: a breakdown must not halve a settled tempo
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
        self.low_bins = freqs < LOW_HZ

    @property
    def seconds(self) -> float:
        return min(self.filled, self.capacity) / self.fps

    def reset(self) -> None:
        self.filled = 0
        self.buffer = np.zeros(0, dtype=np.float32)
        self.prev_log = None
        self.last_bpm = None

    def feed(self, samples: np.ndarray, now: float) -> None:
        """`now` is the wall-clock time at which the last sample in `samples` was heard."""
        self.buffer = np.concatenate([self.buffer, samples.astype(np.float32, copy=False)])
        total = len(self.buffer)
        consumed = 0
        while total - consumed >= self.n_fft:
            frame = self.buffer[consumed:consumed + self.n_fft]
            self._push(frame)
            consumed += self.hop
            # Time of this frame's last sample: `now` minus what is still unread after it.
            self.last_frame_time = now - (total - (consumed - self.hop + self.n_fft)) / self.sr
        self.buffer = self.buffer[consumed:]

    def _push(self, frame: np.ndarray) -> None:
        spectrum = np.abs(np.fft.rfft(frame * self.window))
        log_spec = np.log1p(20.0 * spectrum)
        if self.prev_log is not None:
            rise = np.maximum(log_spec - self.prev_log, 0.0)
            flux = float(rise.sum())
            kick = float(rise[self.low_bins].sum())
        else:
            flux = kick = 0.0
        self.prev_log = log_spec
        power = spectrum * spectrum
        total = float(power.sum()) + 1e-9
        i = self.filled % self.capacity
        self.onset[i] = flux
        self.kick[i] = kick
        self.low[i] = float(power[self.low_bins].sum()) / total
        self.rms[i] = float(np.sqrt(np.mean(frame * frame)))
        self.filled += 1

    def _recent(self, ring: np.ndarray, frames: int) -> np.ndarray:
        n = min(self.filled, self.capacity, frames)
        end = self.filled % self.capacity
        idx = (np.arange(end - n, end)) % self.capacity
        return ring[idx]

    def estimate(self, now: float) -> Tempo | None:
        if self.seconds < 4.0:
            return None
        env = self._recent(self.onset, self.capacity).astype(np.float64)
        env = env - env.mean()
        n = len(env)
        ac = np.correlate(env, env, mode="full")[n - 1:]
        if ac[0] <= 1e-9:
            return None
        ac = ac / ac[0]
        lags = np.arange(n)
        min_lag = int(math.floor(60.0 * self.fps / MAX_BPM))
        max_lag = int(math.ceil(60.0 * self.fps / MIN_BPM))
        candidates = lags[min_lag:max_lag + 1]
        bpm_c = 60.0 * self.fps / candidates
        prior = np.exp(-0.5 * (np.log2(bpm_c / PRIOR_BPM) / PRIOR_SIGMA_OCTAVES) ** 2)
        scores = ac[candidates] * (0.6 + 0.4 * prior)
        if self.last_bpm is not None:
            # Continuity: during a breakdown the kick drops out and the hi-hats suggest half
            # the tempo. Favour candidates near the tempo we were already confident about.
            near = np.exp(-0.5 * (np.log2(bpm_c / self.last_bpm) / 0.08) ** 2)
            scores = scores * (1.0 + CONTINUITY_BONUS * near)
        best = int(candidates[int(np.argmax(scores))])
        lag = self._refine_peak(ac, best)
        period = lag / self.fps
        bpm = 60.0 / period
        confidence = float(np.clip(self._interp(ac, lag), 0.0, 1.0))
        if confidence >= 0.5:
            self.last_bpm = bpm
        multiples = [self._interp(ac, lag * k) for k in (1.0, 2.0, 4.0) if lag * k < n - 1]
        evenness = float(np.clip(np.mean(multiples), 0.0, 1.0)) if multiples else confidence

        # Phase: which offset from the newest frame lines the beat grid up with the onsets.
        # Kick-band onsets lead: hats and snares sit on the off-beats and are brighter, so a
        # broadband envelope alone would happily lock half a beat late.
        recent = self._recent(self.onset, int(4.0 * self.fps)).astype(np.float64)
        kicks = self._recent(self.kick, len(recent)).astype(np.float64)
        m = len(recent)
        grid_env = kicks / (kicks.max() + 1e-9) + 0.25 * recent / (recent.max() + 1e-9)
        lag_i = max(1, int(round(lag)))
        best_offset, best_score = 0, -1.0
        for offset in range(lag_i):
            positions = np.arange(m - 1 - offset, -1, -lag_i)
            score = float(grid_env[positions].sum()) / max(len(positions), 1)
            if score > best_score:
                best_score, best_offset = score, offset
        last_beat = self.last_frame_time - best_offset / self.fps
        if now > last_beat:
            last_beat += period * math.floor((now - last_beat) / period)
        next_beat = last_beat + period

        threshold = recent.mean() + recent.std()
        density = float((recent > threshold).sum()) / (m / self.fps)
        low_ratio = float(self._recent(self.low, m).mean())
        rms = float(self._recent(self.rms, m).mean())
        loudness_db = 20.0 * math.log10(max(rms, 1e-6))
        return Tempo(bpm, period, confidence, next_beat, evenness, low_ratio, density, loudness_db)

    @staticmethod
    def _refine_peak(ac: np.ndarray, i: int) -> float:
        """Parabolic interpolation around integer lag i for a fractional period."""
        if i <= 0 or i >= len(ac) - 1:
            return float(i)
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
