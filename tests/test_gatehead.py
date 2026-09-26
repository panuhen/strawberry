"""The gate's trained head (WIRING.md §8a, gatehead.py): the fit, the calibration, the file, where
heads live, and the gate using one or falling back to the nearest examples."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import numpy as np
import pytest

from strawberry_crab import gatehead, paths
from strawberry_crab.config import Config, ConfigError, GateConfig, _validate, default_toml
from strawberry_crab.gatehead import Head, HeadError, Heads, QuestionHead, Sample
from strawberry_crab.systemone import IS_SENSITIVE, ROUTING, Gate, _options, normalise

from tests.test_systemone import FakeEmbedder, bag_embed

QUESTIONS = {q.name: [o.name for o in _options(q)] for q in (*ROUTING, IS_SENSITIVE)}


def blobs(k: int = 3, n: int = 90, d: int = 16, spread: float = 0.6, seed: int = 0):
    """k separable clusters on the unit sphere, and a group per row (three rows a group)."""
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(k, d))
    y = np.arange(n) % k
    x = centres[y] + spread * rng.normal(size=(n, d))
    return normalise(x), y, [f"g{i // 3}" for i in range(n)]


def test_fit_separates_what_is_separable_and_generalises():
    x, y, _ = blobs()
    weights, bias = gatehead.fit(x, y, 3, 0.01)
    assert weights.shape == (3, 16) and bias.shape == (3,)
    assert ((x @ weights.T + bias).argmax(axis=1) == y).mean() == 1.0
    fresh, fresh_y, _ = blobs(n=60, seed=0)     # the same centres, new noise
    assert ((fresh @ weights.T + bias).argmax(axis=1) == fresh_y).mean() > 0.9


def test_the_gradient_is_the_losss():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=(30, 5)), rng.integers(0, 3, 30)
    w = gatehead.balanced_weights(y, 3)
    params = rng.normal(size=3 * 5 + 3)
    _, grad = gatehead._loss(params, x, y, w, 3, 0.1)
    eye = np.eye(len(params)) * 1e-6
    numeric = [(gatehead._loss(params + e, x, y, w, 3, 0.1)[0] - gatehead._loss(params - e, x, y, w, 3, 0.1)[0]) / 2e-6
               for e in eye]
    assert np.allclose(grad, numeric, atol=1e-6)


def test_balanced_weights_give_every_class_the_same_total():
    y = np.array([0] * 90 + [1] * 10)
    w = gatehead.balanced_weights(y, 2)
    assert w[y == 0].sum() == pytest.approx(w[y == 1].sum())


def test_temperature_cools_an_overconfident_model_and_warms_a_timid_one():
    rng = np.random.default_rng(2)
    y = rng.integers(0, 2, 400)
    truth = np.where(y == 1, 1.0, -1.0)[:, None] * np.array([[-0.5, 0.5]])
    noisy = truth + rng.normal(scale=0.6, size=(400, 2))
    assert gatehead.fit_temperature(noisy * 10, y) > 3          # far too sure: T well above 1
    assert gatehead.fit_temperature(noisy * 0.1, y) < 0.3       # far too timid: T well below 1
    t = gatehead.fit_temperature(noisy, y)
    assert gatehead.nll(noisy, y, t) <= gatehead.nll(noisy, y, 1.0) + 1e-9


def test_folds_keep_a_group_together_and_are_stable():
    groups = [f"g{i % 7}" for i in range(70)]
    first = gatehead.folds(groups)
    assert (first == gatehead.folds(groups)).all()
    for g in set(groups):
        assert len({int(f) for f, h in zip(first, groups) if h == g}) == 1


def test_thresholds_walk_down_until_the_precision_falls_short():
    conf = np.linspace(0, 1, 101)
    correct = conf >= 0.42                    # right above 0.42, wrong below
    assert gatehead.tune_threshold(conf, correct, 0.98) == 0.45
    assert gatehead.tune_threshold(conf, correct, 0.98, floor=0.6) == 0.6
    assert gatehead.tune_threshold(conf, np.zeros(101, bool), 0.98) == 1.0


def fake_samples() -> tuple[list[Sample], np.ndarray]:
    """Every question's own examples as training rows, embedded by the fake embedder."""
    samples = []
    for q in (*ROUTING, IS_SENSITIVE):
        for option in _options(q):
            for i, text in enumerate(option.examples):
                samples.append(Sample(text, {q.name: option.name}, f"{q.name}.{option.name}.{i}"))
    vectors = normalise([bag_embed(s.text) for s in samples])
    return samples, vectors


@pytest.fixture(scope="module")
def fake_head() -> Head:
    samples, vectors = fake_samples()
    return gatehead.train(samples, vectors, QUESTIONS, embedder="embeddinggemma", query_prefix="",
                          dataset="test", l2_grid=(0.001, 0.01))


def test_training_gives_a_calibrated_head_with_its_own_thresholds(fake_head):
    assert set(fake_head.questions) == set(QUESTIONS)
    for name, q in fake_head.questions.items():
        assert q.options == tuple(QUESTIONS[name]) and q.temperature > 0
        report = fake_head.meta["questions"][name]
        assert report["l2"] in (0.001, 0.01) and report["rows"] > 0
    assert 0.0 <= fake_head.offer <= fake_head.act <= 1.0
    assert fake_head.meta["thresholds"]["act"] == fake_head.act
    assert fake_head.dims == 64 and fake_head.version.endswith(gatehead.fingerprint(fake_head))


def test_training_refuses_a_question_without_rows_for_an_option():
    samples = [Sample("a", {"is_urgent": "yes"}, "g")]
    with pytest.raises(HeadError, match="no training rows"):
        gatehead.train(samples, normalise([[1.0, 0.0]]), {"is_urgent": ["yes", "no"]}, embedder="e",
                       query_prefix="", dataset="")


def test_a_head_file_round_trips(tmp_path, fake_head):
    path = fake_head.save(tmp_path / "head.npz")
    again = gatehead.load(path)
    assert again.version == fake_head.version and again.act == fake_head.act and again.path == path
    vector = normalise([bag_embed("skip this song")])[0]
    for name in QUESTIONS:
        assert again.questions[name].probabilities(vector) == pytest.approx(
            fake_head.questions[name].probabilities(vector), abs=1e-5)
    assert path.stat().st_size < 200_000


def test_a_broken_or_foreign_head_file_is_refused(tmp_path, fake_head):
    with pytest.raises(HeadError, match="does not exist"):
        gatehead.load(tmp_path / "missing.npz")
    (tmp_path / "junk.npz").write_bytes(b"not a zip")
    with pytest.raises(HeadError):
        gatehead.load(tmp_path / "junk.npz")
    path = fake_head.save(tmp_path / "head.npz")
    with np.load(path) as data:
        arrays = dict(data)
    meta = json.loads(str(arrays["meta"]))
    meta["format"] = 99
    arrays["meta"] = np.array(json.dumps(meta))
    np.savez(tmp_path / "future.npz", **arrays)
    with pytest.raises(HeadError, match="format 99"):
        gatehead.load(tmp_path / "future.npz")


def test_mismatch_names_the_embedder_the_prefix_the_size_and_the_questions(fake_head):
    assert fake_head.mismatch("embeddinggemma:latest", "", 64, QUESTIONS) == ""
    assert "all-minilm" in fake_head.mismatch("all-minilm", "", 64, QUESTIONS)
    assert "query prefix" in fake_head.mismatch("embeddinggemma", "query: ", 64, QUESTIONS)
    assert "768" in fake_head.mismatch("embeddinggemma", "", 768, QUESTIONS)
    assert "no head for the question" in fake_head.mismatch("embeddinggemma", "", 64, {**QUESTIONS, "mood": ["a", "b"]})
    renamed = {**QUESTIONS, "kind": ["request", "question", "chat", "noise"]}
    assert "kind" in fake_head.mismatch("embeddinggemma", "", 64, renamed)


def test_heads_dir_versions_and_the_current_pointer(tmp_path, fake_head):
    heads = Heads(tmp_path / "heads")
    assert heads.current() is None and heads.versions() == []
    first = heads.add(fake_head)
    assert heads.current() is None                     # written, not in use
    heads.use(first)
    assert heads.current() == first and heads.pointer.read_text().strip() == first.name
    second_head = replace(fake_head, version="20990101-000000-deadbeef")
    second = heads.add(second_head, activate=True)
    assert heads.current() == second and heads.versions() == [first, second]
    assert heads.find(fake_head.version) == first      # a rollback
    heads.use(None)
    assert heads.current() is None
    with pytest.raises(HeadError):
        heads.use(tmp_path / "elsewhere.npz")
    with pytest.raises(HeadError):
        heads.find("nope")


def test_resolve_prefers_the_config_then_the_users_current_then_the_shipped(tmp_path, monkeypatch, fake_head):
    shipped = fake_head.save(tmp_path / "shipped.npz")
    monkeypatch.setattr(gatehead, "SHIPPED", shipped)
    heads = Heads(tmp_path / "heads")
    head, source, tried = gatehead.resolve("", heads)
    assert head is not None and source == "shipped" and tried == []
    mine = heads.add(replace(fake_head, version="mine"), activate=True)
    head, source, _ = gatehead.resolve("", heads)
    assert source == "user" and head.version == "mine"
    mine.write_bytes(b"broken")                        # a broken user head: the shipped one, and why
    head, source, tried = gatehead.resolve("", heads)
    assert source == "shipped" and "current head" in tried[0]
    head, source, _ = gatehead.resolve(str(shipped), heads)
    assert source == "config"
    head, source, tried = gatehead.resolve(str(tmp_path / "nope.npz"), heads)
    assert head is None and tried                     # an explicit file that is not there: no quiet substitute


def test_the_default_heads_dir_is_in_the_data_dir():
    assert Heads().directory == paths.data_dir() / "gate" / "heads"


# ----------------------------------------------------------------------------- the gate


def head_config(path, **kw) -> GateConfig:
    return GateConfig(query_prefix="", document_prefix="", scorer="head", head=str(path), **kw)


async def test_the_gate_scores_with_a_matching_head_and_its_thresholds(tmp_path, fake_head):
    path = replace(fake_head, act=0.55, offer=0.2).save(tmp_path / "head.npz")
    gate = Gate(head_config(path), embedder=FakeEmbedder())
    await gate.start()
    assert gate.ready and gate.scorer == "head" and (gate.act, gate.offer) == (0.55, 0.2)
    route = await gate.route("skip this track")
    assert route is not None and route.kind == "request"
    vector = normalise([bag_embed("skip this track")])[0]
    assert route.answers["kind"].probabilities["request"] == pytest.approx(
        fake_head.questions["kind"].probabilities(vector)[0], abs=1e-4)
    scorer = gate.stats()["scorer"]
    assert scorer["configured"] == "head" and scorer["in_use"] == "head" and scorer["fallback"] is None
    assert scorer["head"]["version"] == fake_head.version and scorer["head"]["source"] == "config"
    assert scorer["act"] == 0.55
    p, _ = await gate.sensitive("Your verification code is 123456")
    assert 0.0 <= p <= 1.0
    await gate.close()


@pytest.mark.parametrize("change, reason", [
    (dict(embedder="all-minilm"), "all-minilm"),
    (dict(query_prefix="task: x | query: "), "query prefix"),
    (dict(dims=768), "shape"),
])
async def test_a_head_that_does_not_fit_falls_back_to_nearest_and_says_why(tmp_path, fake_head, caplog, change, reason):
    path = replace(fake_head, **change).save(tmp_path / "head.npz")
    gate = Gate(head_config(path), embedder=FakeEmbedder())
    with caplog.at_level(logging.WARNING, logger="strawberryd.gate"):
        await gate.start()
    assert gate.ready and gate.scorer == "nearest" and gate.systemone.head is None
    assert (gate.act, gate.offer) == (0.6, 0.3)       # the config's, the nearest scorer's
    scorer = gate.stats()["scorer"]
    assert scorer["in_use"] == "nearest" and reason in scorer["fallback"]
    assert any("head is not used" in r.getMessage() for r in caplog.records)
    route = await gate.route("skip this track")
    assert route is not None and route.kind == "request"
    await gate.close()


async def test_a_missing_head_falls_back_to_nearest(tmp_path, monkeypatch):
    monkeypatch.setattr(gatehead, "SHIPPED", tmp_path / "none.npz")
    gate = Gate(GateConfig(query_prefix="", document_prefix="", scorer="head"), embedder=FakeEmbedder())
    await gate.start()
    assert gate.ready and gate.scorer == "nearest" and "does not exist" in gate.stats()["scorer"]["fallback"]
    await gate.close()


async def test_a_question_the_head_does_not_know_falls_back(tmp_path, fake_head):
    partial = replace(fake_head, questions={k: v for k, v in fake_head.questions.items() if k != "is_sensitive"})
    gate = Gate(head_config(partial.save(tmp_path / "head.npz")), embedder=FakeEmbedder())
    await gate.start()
    assert gate.scorer == "nearest" and "is_sensitive" in gate.head_fallback
    await gate.close()


async def test_scorer_nearest_never_loads_a_head(tmp_path, fake_head):
    path = fake_head.save(tmp_path / "head.npz")
    gate = Gate(replace(head_config(path), scorer="nearest"), embedder=FakeEmbedder())
    await gate.start()
    assert gate.scorer == "nearest" and gate.head is None and gate.stats()["scorer"]["fallback"] is None
    await gate.close()


async def test_an_extra_example_anchors_its_own_sentence_under_the_head(tmp_path, fake_head):
    # A phrase under [gate.examples] still fixes that phrase: close to it, the nearest examples answer.
    constant = QuestionHead(fake_head.questions["kind"].options, np.zeros((4, 64)), np.array([0.0, 0.0, 5.0, 0.0]))
    head = replace(fake_head, questions={**fake_head.questions, "kind": constant})   # the head always says chat
    config = head_config(head.save(tmp_path / "head.npz"), examples={"kind.request": ["pop the kettle on sharpish"]})
    gate = Gate(config, embedder=FakeEmbedder())
    await gate.start()
    assert gate.scorer == "head" and gate.stats()["scorer"]["anchors"] == 1
    anchored = await gate.route("pop the kettle on sharpish")
    assert anchored.kind == "request" and gate.systemone.anchored == 1
    other = await gate.route("hello there")
    assert other.kind == "chat"
    await gate.close()


async def test_privacy_fails_closed_under_the_head(tmp_path, fake_head):
    # A head sure a code is clear does not win: the nearest examples' "private" stands.
    options = fake_head.questions["is_sensitive"].options
    assert options == ("yes", "no")
    clear = QuestionHead(options, np.zeros((2, 64)), np.array([-5.0, 5.0]))
    head = replace(fake_head, questions={**fake_head.questions, "is_sensitive": clear})
    gate = Gate(head_config(head.save(tmp_path / "head.npz")), embedder=FakeEmbedder())
    await gate.start()
    assert gate.scorer == "head"
    p, _ = await gate.sensitive("Your verification code is 482913")
    assert p > 0.5                                     # the nearest examples: it is an example of theirs
    near_no = await gate.systemone.ask("Build passed on main", IS_SENSITIVE)
    assert near_no["is_sensitive"].score < 0.5         # both say clear: clear
    await gate.close()


def test_scorer_config_is_validated_and_templated():
    config = Config()
    assert config.gate.scorer in ("head", "nearest") and config.gate.head == ""
    config.gate.scorer = "knn"
    with pytest.raises(ConfigError, match="gate.scorer"):
        _validate(config)
    template = default_toml()
    assert f'scorer = "{GateConfig().scorer}"' in template and '# head = ""' in template
    assert Config().to_dict()["gate"]["scorer"] == GateConfig().scorer


# ----------------------------------------------------------------------------- strawberry gate


@pytest.fixture
def fake_gate(monkeypatch):
    """gatecmd's gates on the fake embedder: what `strawberry gate` does, without the model."""
    from strawberry_crab import gatecmd

    async def gate(config, **overrides):
        g = Gate(replace(GateConfig(query_prefix="", document_prefix=""), **overrides), embedder=FakeEmbedder())
        await g.start()
        return g

    monkeypatch.setattr(gatecmd, "_gate", gate)
    return gatecmd


def small_sets(tmp_path):
    samples, _ = fake_samples()
    data = tmp_path / "train.jsonl"
    data.write_text("".join(json.dumps({"text": s.text, "lang": "en", "group": s.group, "labels": s.labels}) + "\n"
                            for s in samples))
    heldout = tmp_path / "heldout.json"
    heldout.write_text(json.dumps({
        "phrases": [{"text": "skip this song", "kind": "request", "topic": "music", "tool": "skip", "lang": "en"},
                    {"text": "hyvää huomenta", "kind": "chat", "decision": "chat", "about_her": False, "lang": "fi"}],
        "notifications": [{"app": "Bank", "title": "", "body": "Your verification code is 482913", "sensitive": True}]}))
    return data, heldout


async def test_gate_train_writes_a_version_and_compares_it(tmp_path, monkeypatch, fake_gate):
    data, heldout = small_sets(tmp_path)
    monkeypatch.setattr(gatehead, "HELDOUT_SET", heldout)
    monkeypatch.setattr(gatehead, "SHIPPED", tmp_path / "no-shipped.npz")
    lines: list[str] = []
    assert await fake_gate.train(Config(), data, activate=False, output=None, out=lines.append) == 0
    heads = Heads()
    [written] = heads.versions()
    assert heads.current() is None and any("strawberry gate use" in line for line in lines)
    text = "\n".join(lines)
    assert "| scorer | fields |" in text and "| nearest |" in text and "| new " in text
    head = gatehead.load(written)
    assert head.embedder == "embeddinggemma" and head.query_prefix == "" and head.dataset
    assert fake_gate.cache_file("embeddinggemma-injected").exists()          # the vectors are kept for a retrain
    assert await fake_gate.train(Config(), data, activate=True, output=None, out=lines.append) == 0
    # The fit is deterministic: the same data in the same second is the same version, now in use.
    assert heads.current() is not None and gatehead.load(heads.current()).questions["kind"].weights == pytest.approx(
        head.questions["kind"].weights, abs=1e-6)


async def test_gate_train_output_writes_the_file_and_touches_no_pointer(tmp_path, monkeypatch, fake_gate):
    data, heldout = small_sets(tmp_path)
    monkeypatch.setattr(gatehead, "HELDOUT_SET", heldout)
    out = tmp_path / "shipped.npz"
    assert await fake_gate.train(Config(), data, activate=True, output=out, out=lambda _: None) == 0
    assert out.exists() and Heads().current() is None and Heads().versions() == []


async def test_gate_eval_scores_both_scorers(tmp_path, monkeypatch, fake_gate, fake_head):
    _, heldout = small_sets(tmp_path)
    monkeypatch.setattr(gatehead, "SHIPPED", fake_head.save(tmp_path / "shipped.npz"))
    lines: list[str] = []
    assert await fake_gate.eval_command(Config(), "both", "", heldout, misses=True, out=lines.append) == 0
    text = "\n".join(lines)
    assert "| nearest |" in text and f"| head {fake_head.version} |" in text and "sensitive" in text


def test_gate_use_switches_and_rolls_back(fake_head):
    from strawberry_crab import gatecmd

    heads = Heads()
    path = heads.add(fake_head)
    assert gatecmd.use(fake_head.version, out=lambda _: None) == 0 and heads.current() == path
    assert gatecmd.use("shipped", out=lambda _: None) == 0 and heads.current() is None
    (heads.directory / "head-broken.npz").write_bytes(b"x")
    with pytest.raises(HeadError):
        gatecmd.use("broken", out=lambda _: None)
    assert heads.current() is None                     # a broken file is never put in use


def test_the_cli_parses_the_gate_commands():
    from strawberry_crab.cli import parser

    args = parser().parse_args(["gate", "train", "--activate"])
    assert args.command == "gate" and args.gate_command == "train" and args.activate and args.data is None
    args = parser().parse_args(["gate", "eval", "--scorer", "head", "--misses"])
    assert args.gate_command == "eval" and args.scorer == "head" and args.misses
    assert parser().parse_args(["gate", "use", "shipped"]).version == "shipped"
