"""The gate's trained head (WIRING.md §8a, Router v2): per question, a multinomial logistic regression
on the sentence's normalised embeddinggemma vector, in place of the nearest-examples scorer.

    probabilities = softmax((W·v + b) / T)

W and b are fitted on the data set (data/gate_train.jsonl) with L2 and balanced class weights, T per
question on out-of-fold predictions (grouped folds: sentences generated in one call stay together, so
a paraphrase never validates its twin). The TypeSafe confidence is taken on those calibrated
probabilities, and the act / offer thresholds are tuned on the same out-of-fold predictions, so a head
carries its own: weights, temperatures and thresholds are one versioned unit, and a rollback restores
all three.

A head is a small .npz (~100 KB): the question names and options, W, b, T, the embedder it was trained
on, the query prefix, the data set's hash and the out-of-fold scores. `Heads` finds the one in use: the
data dir's `gate/heads/current` pointer, else the head shipped in the package (data/gate_head.npz).
The gate checks a head against its embedder and its questions at start and falls back to the nearest
scorer, saying why, when it does not match.

Training is plain numpy (L-BFGS on the convex loss): no new dependency, a few seconds per question.

A row can also say what a sentence is *not* (`Sample.avoid`): the learning loop's "that reflex was
wrong" (learning.py, WIRING.md §8d). Such a row's loss is -log(1 - p(option)), which pushes that
option down and leaves the others to the rest of the data; it is still convex.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

log = logging.getLogger("strawberryd.gate")

FORMAT = 1
DATA = Path(__file__).parent / "data"
SHIPPED = DATA / "gate_head.npz"
TRAIN_SET = DATA / "gate_train.jsonl"
HELDOUT_SET = DATA / "gate_heldout.json"

# The out-of-fold precision a threshold must reach (tune_thresholds): an `act` that is wrong fires a
# reflex, which is worse than sending the sentence to the thinker, so it is held to more than `offer`.
ACT_PRECISION = 0.98
OFFER_PRECISION = 0.90
ACTIONABLE = ("request", "question")
L2_GRID = (0.003, 0.01, 0.03, 0.1, 0.3)
BASE_WEIGHT = 2.0              # a hand-written gate example counts as two generated sentences
FOLDS = 5


class HeadError(ValueError):
    pass


# ----------------------------------------------------------------------------- the head


@dataclass
class QuestionHead:
    options: tuple[str, ...]
    weights: np.ndarray          # (options, dims), on the unit vector (standardisation folded in)
    bias: np.ndarray             # (options,)
    temperature: float = 1.0

    def logits(self, vector: np.ndarray) -> np.ndarray:
        return (self.weights @ vector + self.bias) / self.temperature

    def probabilities(self, vector: np.ndarray) -> list[float]:
        return softmax_rows(self.logits(vector)[None, :])[0].tolist()


@dataclass
class Head:
    questions: dict[str, QuestionHead]
    embedder: str                # the embedding model it was trained on ("embeddinggemma")
    query_prefix: str            # the prefix its sentences were embedded with
    dims: int
    act: float                   # kind confidence at which a request/question may fire a reflex
    offer: float
    dataset: str = ""            # sha256 of the data set, first 12 hex digits
    version: str = ""
    created: str = ""
    meta: dict[str, Any] = field(default_factory=dict)   # l2, fold scores, counts: for the report
    path: Path | None = None

    def mismatch(self, embedder: str, query_prefix: str, dims: int | None,
                 questions: dict[str, Sequence[str]]) -> str:
        """Why this head cannot score for that embedder and those questions; "" when it can."""
        if normalise_model(embedder) != normalise_model(self.embedder):
            return f"trained on {self.embedder}, the gate embeds with {embedder}"
        if query_prefix != self.query_prefix:
            return f"trained with the query prefix {self.query_prefix!r}, the gate uses {query_prefix!r}"
        if dims is not None and dims != self.dims:
            return f"trained on {self.dims}-dimensional vectors, the embedder gives {dims}"
        for name, options in questions.items():
            mine = self.questions.get(name)
            if mine is None:
                return f"has no head for the question {name}"
            if tuple(options) != mine.options:
                return f"{name}: trained on the options {list(mine.options)}, the gate asks {list(options)}"
        return ""

    # -- the file

    def save(self, path: Path) -> Path:
        arrays: dict[str, np.ndarray] = {}
        for name, q in self.questions.items():
            arrays[f"w.{name}"] = q.weights.astype(np.float32)
            arrays[f"b.{name}"] = q.bias.astype(np.float32)
        meta = {
            "format": FORMAT, "version": self.version, "created": self.created, "embedder": self.embedder,
            "query_prefix": self.query_prefix, "dims": self.dims, "act": self.act, "offer": self.offer,
            "dataset": self.dataset,
            "questions": {n: {"options": list(q.options), "temperature": q.temperature}
                          for n, q in self.questions.items()},
            "meta": self.meta,
        }
        arrays["meta"] = np.array(json.dumps(meta, ensure_ascii=False))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as f:
            np.savez_compressed(f, **arrays)
        tmp.replace(path)
        self.path = path
        return path


def load(path: Path) -> Head:
    """A head file; HeadError when it is missing, unreadable or of another format."""
    try:
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
            if meta.get("format") != FORMAT:
                raise HeadError(f"{path}: format {meta.get('format')!r}, this version reads {FORMAT}")
            questions = {}
            for name, spec in meta["questions"].items():
                weights = np.asarray(data[f"w.{name}"], dtype=np.float64)
                bias = np.asarray(data[f"b.{name}"], dtype=np.float64)
                options = tuple(spec["options"])
                if weights.shape != (len(options), meta["dims"]) or bias.shape != (len(options),):
                    raise HeadError(f"{path}: {name} has the wrong shape")
                questions[name] = QuestionHead(options, weights, bias, float(spec["temperature"]))
    except HeadError:
        raise
    except FileNotFoundError:
        raise HeadError(f"{path} does not exist") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HeadError(f"{path}: {exc}") from exc
    return Head(questions, meta["embedder"], meta["query_prefix"], int(meta["dims"]), float(meta["act"]),
                float(meta["offer"]), meta.get("dataset", ""), meta.get("version", ""), meta.get("created", ""),
                meta.get("meta", {}), path)


def normalise_model(name: str) -> str:
    """Ollama's "embeddinggemma:latest" is the ONNX export's embeddinggemma (§8a, parity 0.99998)."""
    name = name.strip().lower()
    return name[: -len(":latest")] if name.endswith(":latest") else name


# ----------------------------------------------------------------------------- where heads live


class Heads:
    """The user's heads: `<data>/gate/heads/head-<version>.npz` and a `current` file naming the one in
    use. The learning loop writes a new version and points `current` at it; a rollback points it back.
    With no pointer (or a broken one) the shipped head is used."""

    def __init__(self, directory: Path | None = None) -> None:
        from . import paths

        self.directory = directory or paths.gate_heads_dir()

    @property
    def pointer(self) -> Path:
        return self.directory / "current"

    def versions(self) -> list[Path]:
        return sorted(self.directory.glob("head-*.npz")) if self.directory.is_dir() else []

    def current(self) -> Path | None:
        """The file `current` names, or None when there is no pointer."""
        try:
            name = self.pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return self.directory / name if name else None

    def add(self, head: Head, activate: bool = False) -> Path:
        path = head.save(self.directory / f"head-{head.version}.npz")
        if activate:
            self.use(path)
        return path

    def use(self, path: Path | None) -> None:
        """Point `current` at `path` (a file in this directory); None removes the pointer (the shipped head)."""
        if path is None:
            self.pointer.unlink(missing_ok=True)
            return
        if path.parent.resolve() != self.directory.resolve():
            raise HeadError(f"{path} is not in {self.directory}")
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer.with_name("current.tmp")
        tmp.write_text(path.name + "\n", encoding="utf-8")
        tmp.replace(self.pointer)

    def find(self, version: str) -> Path:
        for path in self.versions():
            if path.stem == f"head-{version}" or path.name == version:
                return path
        raise HeadError(f"no head {version!r} in {self.directory}")


def version_of_path(path: Path) -> str:
    """`head-<version>.npz` -> the version."""
    return path.stem[len("head-"):] if path.stem.startswith("head-") else path.stem


def resolve(configured: str = "", heads: Heads | None = None) -> tuple[Head | None, str, list[str]]:
    """The head to use: `[gate] head` when set, else the user's current one, else the shipped one.
    Returns (head or None, where it came from: "config" | "user" | "shipped", what was tried and failed)."""
    tried: list[str] = []
    if configured:
        try:
            return load(Path(configured).expanduser()), "config", tried
        except HeadError as exc:
            tried.append(str(exc))
            return None, "", tried
    heads = heads or Heads()
    current = heads.current()
    if current is not None:
        try:
            return load(current), "user", tried
        except HeadError as exc:
            tried.append(f"the user's current head: {exc}")
    try:
        return load(SHIPPED), "shipped", tried
    except HeadError as exc:
        tried.append(f"the shipped head: {exc}")
    return None, "", tried


# ----------------------------------------------------------------------------- maths


def softmax_rows(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def typesafe(probabilities: np.ndarray) -> np.ndarray:
    """systemone.confidence for each row: (n·p_max − 1)/(n − 1), floored at 0."""
    n = probabilities.shape[1]
    return np.maximum(0.0, (n * probabilities.max(axis=1) - 1.0) / (n - 1))


def balanced_weights(y: np.ndarray, k: int, negative: np.ndarray | None = None) -> np.ndarray:
    """Every class the same total weight, counted on the rows that say what a sentence is; a row
    that only says what it is not (`negative`) weighs 1."""
    positive = np.ones(len(y), bool) if negative is None else ~negative
    counts = np.bincount(y[positive], minlength=k).astype(np.float64)
    per_class = np.where(counts > 0, positive.sum() / (k * np.maximum(counts, 1)), 0.0)
    return np.where(positive, per_class[y], 1.0)


def _log_likelihood(z: np.ndarray, y: np.ndarray, negative: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row: log p(y), or log(1 - p(y)) for a negative row; the probabilities; and the target the
    gradient pulls them to (one-hot, or for a negative row the other options renormalised)."""
    rows = np.arange(len(y))
    z = z - z.max(axis=1, keepdims=True)
    lse = np.log(np.exp(z).sum(axis=1, keepdims=True))
    log_p = z - lse
    p = np.exp(log_p)
    target = np.zeros_like(p)
    target[rows, y] = 1.0
    ell = log_p[rows, y]
    if negative is not None and negative.any():
        rest = z.copy()
        rest[rows, y] = -np.inf
        top = rest.max(axis=1, keepdims=True)
        lse_rest = top + np.log(np.exp(rest - top).sum(axis=1, keepdims=True))
        ell = np.where(negative, (lse_rest - lse)[:, 0], ell)
        target = np.where(negative[:, None], np.exp(rest - lse_rest), target)
    return ell, p, target


def _loss(params: np.ndarray, x: np.ndarray, y: np.ndarray, w: np.ndarray, k: int, l2: float,
          negative: np.ndarray | None = None):
    d = x.shape[1]
    weights = params[: k * d].reshape(k, d)
    bias = params[k * d:]
    ell, p, target = _log_likelihood(x @ weights.T + bias, y, negative)
    total = w.sum()
    loss = -(w * ell).sum() / total + 0.5 * l2 * (weights * weights).sum()
    residual = (p - target) * (w / total)[:, None]
    grad_w = residual.T @ x + l2 * weights
    grad_b = residual.sum(axis=0)
    return loss, np.concatenate((grad_w.ravel(), grad_b))


def lbfgs(fun, x0: np.ndarray, iterations: int = 500, memory: int = 10, tolerance: float = 1e-6) -> np.ndarray:
    """Minimise a smooth convex `fun(x) -> (value, gradient)`: L-BFGS with a backtracking line search."""
    x = x0.copy()
    value, grad = fun(x)
    s_list: list[np.ndarray] = []
    y_list: list[np.ndarray] = []
    for _ in range(iterations):
        if np.linalg.norm(grad, np.inf) < tolerance:
            break
        q = grad.copy()
        alphas = []
        for s, yv in zip(reversed(s_list), reversed(y_list)):
            rho = 1.0 / (yv @ s)
            a = rho * (s @ q)
            alphas.append((rho, a))
            q -= a * yv
        if s_list:
            q *= (s_list[-1] @ y_list[-1]) / (y_list[-1] @ y_list[-1])
        for (s, yv), (rho, a) in zip(zip(s_list, y_list), reversed(alphas)):
            q += (a - rho * (yv @ q)) * s
        direction = -q
        slope = grad @ direction
        if slope >= 0:                                  # not a descent direction: start over
            s_list.clear()
            y_list.clear()
            direction = -grad
            slope = grad @ direction
        step = 1.0
        while True:
            candidate = x + step * direction
            new_value, new_grad = fun(candidate)
            if new_value <= value + 1e-4 * step * slope or step < 1e-10:
                break
            step *= 0.5
        s, yv = candidate - x, new_grad - grad
        if yv @ s > 1e-12:
            s_list.append(s)
            y_list.append(yv)
            if len(s_list) > memory:
                s_list.pop(0)
                y_list.pop(0)
        converged = abs(value - new_value) < 1e-10 * max(1.0, abs(value))
        x, value, grad = candidate, new_value, new_grad
        if converged:
            break
    return x


def fit(x: np.ndarray, y: np.ndarray, k: int, l2: float, sample: np.ndarray | None = None,
        negative: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Weights and bias on the unit vectors `x` for labels `y` (0..k-1): features standardised for the
    fit, the standardisation folded back into W and b, so the head reads the raw unit vector. `sample`
    weighs rows on top of the balanced class weights (the gate's own examples count more); a row in
    `negative` says the sentence is not y."""
    mean = x.mean(axis=0)
    scale = x.std(axis=0) + 1e-6
    xs = (x - mean) / scale
    w = balanced_weights(y, k, negative) * (sample if sample is not None else 1.0)
    d = x.shape[1]
    params = lbfgs(lambda p: _loss(p, xs, y, w, k, l2, negative), np.zeros(k * d + k))
    weights = params[: k * d].reshape(k, d)
    bias = params[k * d:]
    folded = weights / scale
    return folded, bias - folded @ mean


def nll(logits: np.ndarray, y: np.ndarray, temperature: float, w: np.ndarray | None = None,
        negative: np.ndarray | None = None) -> float:
    picked, _, _ = _log_likelihood(logits / temperature, y, negative)
    if w is None:
        return float(-picked.mean())
    return float(-(w * picked).sum() / w.sum())


def fit_temperature(logits: np.ndarray, y: np.ndarray, w: np.ndarray | None = None,
                    negative: np.ndarray | None = None) -> float:
    """The T that minimises the (weighted) log loss of softmax(logits / T): golden section on log T."""
    lo, hi = math.log(0.05), math.log(20.0)
    ratio = (math.sqrt(5) - 1) / 2
    a, b = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
    fa, fb = nll(logits, y, math.exp(a), w, negative), nll(logits, y, math.exp(b), w, negative)
    for _ in range(60):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - ratio * (hi - lo)
            fa = nll(logits, y, math.exp(a), w, negative)
        else:
            lo, a, fa = a, b, fb
            b = lo + ratio * (hi - lo)
            fb = nll(logits, y, math.exp(b), w, negative)
    return round(math.exp((lo + hi) / 2), 4)


def folds(groups: Sequence[str], n: int = FOLDS) -> np.ndarray:
    """A fold per row, the same for every row of a group, stable across runs (a hash of the group)."""
    return np.array([int(hashlib.sha256(g.encode()).hexdigest()[:8], 16) % n for g in groups])


def out_of_fold(x: np.ndarray, y: np.ndarray, k: int, l2: float, fold: np.ndarray,
                sample: np.ndarray | None = None, negative: np.ndarray | None = None) -> np.ndarray:
    """Logits for every row from a head fitted without its fold."""
    logits = np.zeros((len(y), k))
    if len(np.unique(fold)) < 2:
        raise HeadError("every row is in one fold: too few groups to validate on")
    positive = np.ones(len(y), bool) if negative is None else ~negative
    for f in np.unique(fold):
        held = fold == f
        if len(np.unique(y[~held & positive])) < k:
            raise HeadError(f"fold {f} leaves an option without training rows: too few groups for it")
        weights, bias = fit(x[~held], y[~held], k, l2, sample[~held] if sample is not None else None,
                            negative[~held] if negative is not None else None)
        logits[held] = x[held] @ weights.T + bias
    return logits


def tune_threshold(confidence: np.ndarray, correct: np.ndarray, precision: float, floor: float = 0.0,
                   minimum: int = 20) -> float:
    """The lowest threshold (0.05 steps) at and above which the rows over it are right at least
    `precision` of the time, never below `floor`. Walked down from 1: the first step that falls short
    stops it. A step with fewer than `minimum` rows over it proves nothing and is passed over."""
    chosen = 1.0
    for t in (round(0.05 * i, 2) for i in range(20, 0, -1)):
        above = confidence >= t
        if above.sum() < minimum:
            continue
        if correct[above].mean() < precision:
            break
        chosen = t
    return max(chosen, floor)


# ----------------------------------------------------------------------------- training


@dataclass
class Sample:
    text: str
    labels: dict[str, str]       # question -> option; a question the sentence does not answer is absent
    group: str                   # generation call: rows of one group share a fold
    weight: float = 1.0          # BASE_WEIGHT for the gate's own examples
    avoid: dict[str, str] = field(default_factory=dict)      # question -> an option the sentence is not
    weights: dict[str, float] = field(default_factory=dict)  # question -> weight, in place of `weight`

    def weight_for(self, question: str) -> float:
        return self.weights.get(question, self.weight)


def read_dataset(path: Path = TRAIN_SET) -> tuple[list[Sample], str]:
    raw = path.read_bytes()
    samples = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        labels = {q: v for q, v in row["labels"].items() if v}
        weight = BASE_WEIGHT if row.get("source", "generated") != "generated" else 1.0
        samples.append(Sample(row["text"], labels, row.get("group") or row["text"], weight))
    return samples, hashlib.sha256(raw).hexdigest()[:12]


def train(samples: Sequence[Sample], vectors: np.ndarray, questions: dict[str, Sequence[str]], *,
          embedder: str, query_prefix: str, dataset: str, l2_grid: Iterable[float] = L2_GRID,
          progress=None) -> Head:
    """One head per question: L2 chosen by out-of-fold log loss, T fitted on the out-of-fold logits,
    then the final fit on every row. `vectors` are the samples' unit vectors (query prefix included)."""
    fold_all = folds([s.group for s in samples])
    heads: dict[str, QuestionHead] = {}
    report: dict[str, Any] = {"questions": {}}
    oof: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}   # question -> (rows, y, calibrated p)
    for name, options in questions.items():
        started = time.perf_counter()
        index = {o: i for i, o in enumerate(options)}
        rows = np.array([i for i, s in enumerate(samples)
                         if s.labels.get(name) in index or s.avoid.get(name) in index])
        if len(rows) == 0:
            raise HeadError(f"no training rows for {name}")
        # A row that says what the sentence is wins over one that says what it is not.
        negative = np.array([samples[i].labels.get(name) not in index for i in rows])
        y = np.array([index[samples[i].labels[name]] if not neg else index[samples[i].avoid[name]]
                      for i, neg in zip(rows, negative)])
        positive = ~negative
        missing = [o for o in options if index[o] not in set(y[positive].tolist())]
        if missing:
            raise HeadError(f"{name}: no training rows for {missing}")
        x, fold, k = vectors[rows], fold_all[rows], len(options)
        sample = np.array([samples[i].weight_for(name) for i in rows])
        neg = negative if negative.any() else None
        w = balanced_weights(y, k, neg)
        best = None
        for l2 in l2_grid:
            logits = out_of_fold(x, y, k, l2, fold, sample, neg)
            t = fit_temperature(logits, y, w, neg)
            loss = nll(logits, y, t, w, neg)
            if best is None or loss < best[0]:
                best = (loss, l2, t, logits)
        loss, l2, temperature, logits = best
        right = np.where(positive, logits.argmax(axis=1) == y, logits.argmax(axis=1) != y)
        if right.all():
            # Not one answer wrong out of fold: the log loss falls as T does, to the edge of the search.
            # Nothing to calibrate against, so the fit's own scale stands.
            temperature = 1.0
            loss = nll(logits, y, temperature, w, neg)
        p = softmax_rows(logits / temperature)
        oof[name] = (rows[positive], y[positive], p[positive])
        weights, bias = fit(x, y, k, l2, sample, neg)
        heads[name] = QuestionHead(tuple(options), weights, bias, temperature)
        yp, pp = y[positive], p[positive]
        accuracy = float((pp.argmax(axis=1) == yp).mean())
        balanced = float(np.mean([(pp.argmax(axis=1)[yp == c] == c).mean() for c in range(k)]))
        report["questions"][name] = {"rows": int(len(rows)), "l2": l2, "temperature": temperature,
                                     "oof_accuracy": round(accuracy, 4), "oof_balanced": round(balanced, 4),
                                     "oof_nll": round(loss, 4),
                                     "counts": {o: int((yp == i).sum()) for o, i in index.items()}}
        if neg is not None:
            report["questions"][name]["avoid"] = {"rows": int(negative.sum()),
                                                  "oof_kept_off": round(float(right[negative].mean()), 4)}
        if progress:
            progress(f"{name}: {len(rows)} rows, L2 {l2}, T {temperature}, out-of-fold {accuracy:.3f} "
                     f"({time.perf_counter() - started:.1f}s)")
    act, offer, extra = thresholds(samples, oof, questions)
    report["thresholds"] = extra
    created = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    head = Head(heads, embedder, query_prefix, int(vectors.shape[1]), act, offer, dataset, "", created, report)
    head.version = time.strftime("%Y%m%d-%H%M%S") + "-" + fingerprint(head)
    return head


def thresholds(samples: Sequence[Sample], oof: dict, questions: dict[str, Sequence[str]]) -> tuple[float, float, dict]:
    """act and offer on the out-of-fold `kind`: the lowest confidences at which a request/question
    reading is right ACT_PRECISION and OFFER_PRECISION of the time. Also what the reflex tier would
    make of it at [actions] reflex 0.6, for the report."""
    rows, y, p = oof["kind"]
    options = list(questions["kind"])
    predicted = p.argmax(axis=1)
    actionable = np.isin(np.array(options)[predicted], ACTIONABLE)
    conf = typesafe(p)
    correct = predicted == y
    act = tune_threshold(conf[actionable], correct[actionable], ACT_PRECISION)
    offer = min(act, tune_threshold(conf[actionable], correct[actionable], OFFER_PRECISION))
    fired = actionable & (conf >= act)
    extra = {"act": act, "offer": offer, "act_precision": ACT_PRECISION, "offer_precision": OFFER_PRECISION,
             "fired": int(fired.sum()), "fired_wrong": int((fired & ~correct).sum()),
             "actionable": int(actionable.sum())}
    return act, offer, extra


def fingerprint(head: Head) -> str:
    digest = hashlib.sha256()
    for name in sorted(head.questions):
        q = head.questions[name]
        digest.update(name.encode())
        digest.update(np.ascontiguousarray(q.weights, dtype=np.float32).tobytes())
        digest.update(np.ascontiguousarray(q.bias, dtype=np.float32).tobytes())
        digest.update(repr(q.temperature).encode())
    return digest.hexdigest()[:8]
