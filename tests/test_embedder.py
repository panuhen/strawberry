"""The gate's in-process embedder (embedder.py), the config that picks it, and the fall back to
Ollama when its files are missing. The tests that load the real model skip without it: point
STRAWBERRY_GATE_MODEL at a directory with the fp32 files (`python -m strawberry_crab.embedder
--fetch --dir DIR`), or have them in the default data dir."""

from __future__ import annotations

import asyncio
import json
import os
import tomllib
from pathlib import Path

import numpy as np
import pytest

from strawberry_crab import embedder, paths, systemone
from strawberry_crab.config import Config, ConfigError, GateConfig, _validate, default_toml
from strawberry_crab.embedder import OnnxEmbedder
from strawberry_crab.systemone import Gate, GateError, normalise
from tests.test_systemone import FakeEmbedder

HERE = Path(__file__).parent


def real_model_dir() -> Path | None:
    """The fp32 files, if this machine has them. The XDG dirs are throwaway in tests, so the user's
    default data dir is worked out from the home directory."""
    candidates = [os.environ.get("STRAWBERRY_GATE_MODEL", "")]
    if os.name != "nt":
        candidates.append(str(Path.home() / ".local/share/strawberry/models/embeddinggemma-300m-onnx"))
    for candidate in filter(None, candidates):
        if not embedder.missing(Path(candidate)):
            return Path(candidate)
    return None


MODEL = real_model_dir()
needs_model = pytest.mark.skipif(MODEL is None, reason="the gate's ONNX model is not on this machine")


def sparse_model(directory: Path) -> None:
    """Files at the published sizes and nothing in them: enough for `missing`, not for ONNX Runtime."""
    for name, (size, _) in embedder.FILES.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        with (directory / name).open("wb") as f:
            f.truncate(size)


class FakeOllama(FakeEmbedder):
    """Stands in for systemone.OllamaEmbedder in the fallback tests."""

    made: list[tuple] = []

    def __init__(self, model: str, url: str, timeout_s: float) -> None:
        super().__init__()
        FakeOllama.made.append((model, url, timeout_s))

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_ollama(monkeypatch):
    FakeOllama.made = []
    monkeypatch.setattr(systemone, "OllamaEmbedder", FakeOllama)
    return FakeOllama


# --- config ---------------------------------------------------------------------------


def test_config_defaults_and_validation():
    gate = Config().gate
    assert (gate.embedder, gate.onnx_dir, gate.onnx_threads) == ("onnx", "", 4)
    for bad in (dict(embedder="cuda"), dict(onnx_threads=0), dict(onnx_threads=65)):
        config = Config()
        for key, value in bad.items():
            setattr(config.gate, key, value)
        with pytest.raises(ConfigError, match=f"gate.{next(iter(bad))}"):
            _validate(config)
    config = Config()
    config.gate.embedder = "ollama"
    _validate(config)


def test_the_template_and_get_config_carry_the_embedder():
    gate = tomllib.loads(default_toml())["gate"]
    assert (gate["embedder"], gate["onnx_threads"]) == ("onnx", 4)
    assert "onnx_dir" in default_toml()                            # commented, with the default path
    assert Config().to_dict()["gate"]["embedder"] == "onnx"         # what GET /config returns


def test_the_model_dir_is_in_the_data_dir_unless_configured(tmp_path):
    assert embedder.model_dir() == paths.data_dir() / "models" / "embeddinggemma-300m-onnx"
    assert embedder.model_dir(str(tmp_path)) == tmp_path


# --- files and loading ------------------------------------------------------------------


def test_missing_names_absent_and_short_files(tmp_path):
    assert embedder.missing(tmp_path) == list(embedder.FILES)
    sparse_model(tmp_path)
    assert embedder.missing(tmp_path) == []
    (tmp_path / "tokenizer.json").write_bytes(b"cut short")        # an interrupted download
    assert embedder.missing(tmp_path) == ["tokenizer.json"]


async def test_a_load_without_the_files_or_with_broken_ones_is_a_gate_error(tmp_path):
    emb = OnnxEmbedder(tmp_path)
    with pytest.raises(GateError, match="not in"):
        await emb.start()
    sparse_model(tmp_path)
    with pytest.raises(GateError, match="could not load"):
        await emb.start()
    assert not emb.loaded
    with pytest.raises(GateError, match="not started"):
        await emb(["hello"])


# --- the gate: which backend, and the fallback -------------------------------------------


async def test_the_gate_falls_back_to_ollama_without_the_model(tmp_path, fake_ollama, caplog):
    gate = Gate(GateConfig(onnx_dir=str(tmp_path), query_prefix="", document_prefix=""), "http://127.0.0.1:1")
    assert gate.backend == "onnx"
    await gate.start()
    assert gate.ready and gate.backend == "ollama"                   # never a crash, and the gate works
    assert fake_ollama.made == [("embeddinggemma", "http://127.0.0.1:1", 2.0)]
    assert gate.systemone.embed is gate.owned
    stats = gate.stats()["embedder"]
    assert stats["backend"] == "ollama" and stats["configured"] == "onnx"
    assert "tokenizer.json" in stats["fallback"] and str(tmp_path) in stats["fallback"]
    assert "using embeddinggemma through Ollama instead" in caplog.text
    assert await gate.route("skip this song") is not None
    await gate.close()


async def test_health_says_the_gate_fell_back(tmp_path, fake_ollama, aiohttp_client):
    from strawberry_crab.daemon import Daemon
    from strawberry_crab.events import CannedReactor
    from strawberry_crab.server import create_app

    config = Config()
    config.brain.enabled = config.speech.enabled = config.voice.enabled = config.actions.mpris = False
    config.thinker.enabled = False
    config.gate.onnx_dir = str(tmp_path)
    daemon = Daemon(reactor=CannedReactor(), config=config)
    client = await aiohttp_client(create_app(daemon))       # on_startup starts the gate, in the background
    await asyncio.wait_for(daemon.gate.starting, 5)
    gate = (await (await client.get("/health")).json())["gate"]
    assert gate["ready"] and gate["embedder"]["backend"] == "ollama"
    assert gate["embedder"]["fallback"].endswith(f"not in {tmp_path}")
    assert (await (await client.get("/config")).json())["gate"]["embedder"] == "onnx"


async def test_embedder_ollama_is_ollama_from_the_start(fake_ollama):
    gate = Gate(GateConfig(embedder="ollama"), "http://127.0.0.1:1")
    assert gate.backend == "ollama" and isinstance(gate.owned, FakeOllama)
    await gate.start()
    assert gate.stats()["embedder"] == {"backend": "ollama", "configured": "ollama", "fallback": None}
    await gate.close()


def test_an_injected_embedder_and_a_disabled_gate():
    assert Gate(GateConfig(), embedder=FakeEmbedder()).stats()["embedder"]["backend"] == "injected"
    assert Gate(GateConfig(enabled=False)).stats()["embedder"] is None


# --- the download (no network: the hub's download is faked) -------------------------------


def test_fetch_keeps_real_files_and_checks_them(tmp_path, monkeypatch):
    import hashlib

    import huggingface_hub

    payload = {name: name.encode() * 3 for name in embedder.FILES}
    monkeypatch.setattr(embedder, "FILES", {name: (len(data), hashlib.sha256(data).hexdigest())
                                            for name, data in payload.items()})
    asked = []

    def download(repo, name, revision, local_dir):
        asked.append((repo, name, revision))
        path = Path(local_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload[name])
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    assert embedder.fetch(tmp_path, say=lambda line: None) == tmp_path
    assert [a[1] for a in asked] == list(embedder.FILES) and {a[0] for a in asked} == {embedder.REPO}
    assert {a[2] for a in asked} == {embedder.REVISION}
    assert embedder.missing(tmp_path) == [] and not any((tmp_path / n).is_symlink() for n in embedder.FILES)
    asked.clear()
    embedder.fetch(tmp_path, say=lambda line: None)                # all there: nothing downloaded
    assert asked == []
    payload["tokenizer.json"] = b"x" * len(payload["tokenizer.json"])   # same size, other bytes
    (tmp_path / "tokenizer.json").unlink()
    with pytest.raises(embedder.FetchError, match="sha256"):
        embedder.fetch(tmp_path, say=lambda line: None)
    assert not (tmp_path / "tokenizer.json").exists()


# --- the real model -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def onnx():
    emb = OnnxEmbedder(MODEL, threads=4)
    emb.load()
    return emb


@needs_model
def test_vectors_are_unit_length_in_order_and_batched_as_one_by_one(onnx):
    texts = [f"title: none | text: {'a longer sentence ' * (i % 7)}number {i}" for i in range(40)]  # > one batch
    batch = onnx.embed(texts)
    assert batch.shape == (40, 768) and batch.dtype == np.float32 and np.isfinite(batch).all()
    assert np.allclose(np.linalg.norm(batch, axis=1), 1.0, atol=1e-4)
    single = np.concatenate([onnx.embed([t]) for t in texts[:6]])
    assert ((batch[:6] * single).sum(1) > 0.99999).all()            # padding changes nothing
    assert onnx.load_s is not None and onnx.stats()["threads"] == 4


@needs_model
async def test_the_same_similarities_as_ollama(onnx):
    """Against Ollama's embeddinggemma, recorded (tests/gate_parity.json): the in-process vectors
    are cosine 0.99998+ to Ollama's, so the similarities the gate scores agree to ~1e-3."""
    recorded = json.loads((HERE / "gate_parity.json").read_text(encoding="utf-8"))
    config = GateConfig()
    queries = normalise(await onnx([config.query_prefix + t for t in recorded["queries"]]))
    documents = normalise(await onnx([config.document_prefix + t for t in recorded["documents"]]))
    assert np.abs(queries @ documents.T - np.array(recorded["similarities"])).max() < 3e-3


@needs_model
async def test_a_gate_on_the_real_model_routes_the_plain_commands(fake_ollama):
    gate = Gate(GateConfig(onnx_dir=str(MODEL)))
    await gate.start()
    try:
        assert gate.ready and gate.backend == "onnx" and fake_ollama.made == []
        assert gate.stats()["embedder"]["load_s"] > 0
        skip = await gate.route("skip this song")
        assert (skip.kind, skip.topic, skip.tool, skip.decision) == ("request", "music", "skip", "act")
        chat = await gate.route("how are you doing today")
        assert chat.kind == "chat"
    finally:
        await gate.close()
