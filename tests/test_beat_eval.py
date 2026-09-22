"""scripts/beat_eval.py still runs, and the tracker keeps its scores on the generated set."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "beat_eval.py"


@pytest.fixture(scope="module")
def beat_eval():
    spec = importlib.util.spec_from_file_location("beat_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["beat_eval"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("beat_eval", None)


def test_generated_set_scores(beat_eval):
    # 20 s clips instead of the script's 30 s: the same patterns, a third less time.
    clips = beat_eval.synthetic_set(20.0)
    assert len(clips) >= 30
    tracker = beat_eval.load_tracker(None)
    summary = beat_eval.summarise([beat_eval.run_clip(tracker, c) for c in clips])
    # Numbers from WIRING.md §4c with some slack; the first tracker scored 0.56 / 0.82 here.
    assert summary["acc1"] >= 0.75, summary
    assert summary["acc2"] >= 0.85, summary
    assert summary["phase_on_beat"] >= 0.85, summary
    assert summary["beatless_claims"] == 0.0, summary
    assert summary["steady_precision"] >= 0.85, summary


def test_scoring_rules(beat_eval):
    assert beat_eval.classify(121.0, 120.0) == "ok"
    assert beat_eval.classify(60.5, 120.0) == "octave"
    assert beat_eval.classify(241.0, 120.0) == "octave"
    assert beat_eval.classify(80.0, 120.0) == "other"
