"""The gate's embeddinggemma in this process: ONNX Runtime on the CPU (WIRING.md §8a).

Through Ollama one embedding call costs ~170 ms, almost all of it the per-call overhead; the
same model in-process is ~19 ms a sentence (4 threads), ~26 ms for a whole route. Measured
2026-09-25 on `onnx-community/embeddinggemma-300m-ONNX`:

    fp32, CPU           the same vectors as Ollama's (cosine 0.99999), gate_check 87/87, ~815 MiB RAM,
                        1.6 s to load
    fp16                NaN: never
    q4                  15 routes change: no
    all the cores       a worse p95 than 4-8 threads, so the threads are capped (`[gate] onnx_threads`)

The graph does the pooling, the dense layers and the normalisation itself and outputs
`sentence_embedding`; the tokenizer adds <bos> and <eos> as Ollama does. The prompt prefixes stay
the gate's (`query_prefix`, `document_prefix`).

The model is not shipped: `strawberry setup` fetches the fp32 files from their publisher into
paths.gate_model_dir() (`python -m strawberry_crab.embedder --fetch`). They must be real files:
ONNX Runtime refuses external weights reached through the Hugging Face cache's symlinks, so the
download goes to its own directory, not the cache. Without them the gate falls back to Ollama.

onnxruntime and tokenizers are imported when the model loads, not with this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .systemone import GateError

log = logging.getLogger("strawberryd.gate")

REPO = "onnx-community/embeddinggemma-300m-ONNX"
REVISION = "5090578d9565bb06545b4552f76e6bc2c93e4a66"   # the export that was measured
MODEL = "onnx/model.onnx"                               # fp32; its weights are the _data file next to it
# The files the embedder reads, with their size and sha256 at REVISION.
FILES = {
    MODEL: (479932, "ea91fd315a7c152d427d231746f0f811a1ac93beaba656abfdf2b24e091265e4"),
    "onnx/model.onnx_data": (1234521088, "ef835ae565d8695236652475903078e8ed794c7c35faf1164d78ec3238e8a88d"),
    "tokenizer.json": (20323312, "4dda02faaf32bc91031dc8c88457ac272b00c1016cc679757d1c441b248b9c47"),
}
LICENCE = "Gemma Terms of Use, https://ai.google.dev/gemma/terms"
SIZE = "1.2 GB"
BATCH = 32              # texts per run: the examples at start go in batches of similar length
MAX_TOKENS = 2048       # embeddinggemma's context; a longer text is cut, as Ollama cuts it


def model_dir(configured: str = "") -> Path:
    from . import paths

    return Path(configured).expanduser() if configured else paths.gate_model_dir()


def missing(directory: Path) -> list[str]:
    """The model's files that are not in `directory` at their full size (an interrupted download
    leaves a short one). Sizes only: hashing 1.2 GB is for the download, not every start."""
    out = []
    for name, (size, _) in FILES.items():
        path = directory / name
        try:
            if not path.is_file() or path.stat().st_size != size:
                out.append(name)
        except OSError:
            out.append(name)
    return out


class OnnxEmbedder:
    """The gate's embedder (async texts -> vectors) with the model loaded in this process.

    Loaded once, by `start` (or `load`); a load that fails raises GateError and the gate falls
    back to Ollama. Calls run in a worker thread, so a batch of examples does not hold up the
    event loop, and any number can be in flight: an ONNX Runtime session and a tokenizer are
    safe to share between threads."""

    backend = "onnx"

    def __init__(self, directory: Path, threads: int = 4) -> None:
        self.directory = directory
        self.threads = threads
        self.session = None
        self.tokenizer = None
        self.inputs: set[str] = set()
        self.load_s: float | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self.session is not None

    def load(self) -> None:
        with self._lock:
            if self.session is not None:
                return
            lacking = missing(self.directory)
            if lacking:
                raise GateError(f"{', '.join(lacking)} not in {self.directory}")
            started = time.perf_counter()
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer

                tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
                tokenizer.no_padding()
                tokenizer.enable_truncation(MAX_TOKENS)
                options = ort.SessionOptions()
                options.log_severity_level = 3
                options.intra_op_num_threads = self.threads
                options.inter_op_num_threads = 1
                session = ort.InferenceSession(str(self.directory / MODEL), options,
                                               providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001  (an import, a broken file, ONNX Runtime refusing it)
                raise GateError(f"could not load {self.directory / MODEL}: {exc}") from exc
            self.inputs = {i.name for i in session.get_inputs()}
            self.tokenizer = tokenizer
            self.session = session
            self.load_s = time.perf_counter() - started
            log.info("gate: embeddinggemma (ONNX, CPU, %d threads) loaded in %.1fs", self.threads, self.load_s)

    async def start(self) -> None:
        await asyncio.to_thread(self.load)

    async def close(self) -> None:
        with self._lock:
            self.session = None
            self.tokenizer = None

    def embed(self, texts: list[str]) -> np.ndarray:
        """One float32 row per text, in order; blocking."""
        session, tokenizer = self.session, self.tokenizer
        if session is None or tokenizer is None:
            raise GateError("embedder not started")
        encodings = tokenizer.encode_batch(list(texts))
        out = np.empty((len(texts), 0), np.float32)
        # Similar lengths together, so a short sentence is not padded out to the longest example.
        order = sorted(range(len(texts)), key=lambda i: len(encodings[i].ids))
        for start in range(0, len(order), BATCH):
            chunk = order[start:start + BATCH]
            width = max(len(encodings[i].ids) for i in chunk)
            ids = np.zeros((len(chunk), width), np.int64)          # <pad> is id 0
            mask = np.zeros((len(chunk), width), np.int64)
            for row, i in enumerate(chunk):
                n = len(encodings[i].ids)
                ids[row, :n] = encodings[i].ids
                mask[row, :n] = 1
            feed = {k: v for k, v in (("input_ids", ids), ("attention_mask", mask)) if k in self.inputs}
            try:
                vectors = session.run(["sentence_embedding"], feed)[0]
            except Exception as exc:  # noqa: BLE001
                raise GateError(f"ONNX Runtime: {exc}") from exc
            if out.shape[1] == 0:
                out = np.empty((len(texts), vectors.shape[1]), np.float32)
            out[chunk] = vectors
        return out

    async def __call__(self, texts: list[str]) -> np.ndarray:
        if not self.loaded:
            raise GateError("embedder not started")
        return await asyncio.to_thread(self.embed, texts)

    def stats(self) -> dict:
        return {"backend": self.backend, "dir": str(self.directory), "threads": self.threads,
                "load_s": round(self.load_s, 2) if self.load_s is not None else None}


# ----------------------------------------------------------------------------- the download


class FetchError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(directory: Path | None = None, say: Callable[[str], None] = print) -> Path:
    """Download the fp32 model and its tokenizer from REPO at REVISION into `directory`, as real
    files (not links into the Hugging Face cache), each checked against its size and sha256. A
    file already there at its full size is kept. Returns the directory."""
    import huggingface_hub

    directory = directory or model_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in FILES.items():
        path = directory / name
        if path.is_file() and not path.is_symlink() and path.stat().st_size == size:
            continue
        say(f"{REPO}: {name}")
        try:
            got = Path(huggingface_hub.hf_hub_download(REPO, name, revision=REVISION, local_dir=directory))
        except Exception as exc:  # noqa: BLE001  (offline, a proxy, the hub's own errors)
            raise FetchError(f"{name}: {exc}") from exc
        if got.is_symlink():                       # never expected with local_dir; ONNX Runtime refuses it
            target = got.resolve()
            got.unlink()
            got.write_bytes(target.read_bytes())
        if got.stat().st_size != size or sha256(got) != digest:
            got.unlink(missing_ok=True)
            raise FetchError(f"{name}: not the file that was published at {REVISION[:12]} (size or sha256)")
    return directory


def main(argv: list[str] | None = None) -> int:
    """`python -m strawberry_crab.embedder --fetch [--dir DIR]`, what `strawberry setup` runs."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m strawberry_crab.embedder")
    parser.add_argument("--fetch", action="store_true", required=True, help="download the gate's ONNX model")
    parser.add_argument("--dir", type=Path, default=None, help="default: " + str(model_dir()))
    args = parser.parse_args(argv)
    try:
        directory = fetch(args.dir)
    except FetchError as exc:
        print(f"could not fetch the gate's model: {exc}", file=sys.stderr)
        return 1
    print(f"embeddinggemma (ONNX, fp32) is in {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
