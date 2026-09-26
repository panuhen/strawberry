"""`strawberry gate train | eval | use`: the gate's trained head by hand (WIRING.md §8a).

    strawberry gate train [--data FILE] [--activate] [--output FILE]
    strawberry gate eval [--scorer head|nearest|both] [--head FILE] [--set FILE] [--misses]
    strawberry gate use VERSION|shipped

`train` embeds the data set (the shipped data/gate_train.jsonl unless --data says otherwise) with the
configured embedder, fits a head (gatehead.train), writes it as a new version in the data dir's heads
dir and prints the held-out scores of the nearest examples, the head in use and the new one side by
side. It becomes the head in use only with --activate (or later with `gate use`). `eval` scores the
configured gate on the held-out set, or on another labelled set. The daemon need not run; restart it
after `use` or `train --activate`.

The data set's vectors are cached in the cache dir (`gate/vectors-<model>-<backend>.npz`): a retrain embeds only
new sentences.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from . import gateeval, gatehead, paths

log = logging.getLogger("strawberryd.gate")


class GateCommandError(RuntimeError):
    pass


def _questions() -> dict[str, list[str]]:
    from .systemone import IS_SENSITIVE, ROUTING, _options

    return {q.name: [o.name for o in _options(q)] for q in (*ROUTING, IS_SENSITIVE)}


async def _gate(config, **overrides):
    """A started gate with the daemon's questions (adapter phrases included), or GateCommandError."""
    from .adapters import gate_examples, load as load_adapters
    from .systemone import Gate

    adapters = load_adapters(config.tools.servers) if config.tools.enabled else {}
    gate = Gate(replace(config.gate, enabled=True, **overrides), config.brain.ollama_url,
                examples=gate_examples(adapters))
    await gate.start()
    if not gate.ready:
        await gate.close()
        raise GateCommandError(f"gate not ready: {gate.disabled_reason}")
    return gate


def cache_file(model: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in gatehead.normalise_model(model))
    return paths.cache_dir() / "gate" / f"vectors-{safe}.npz"


async def embed(gate, texts: list[str], prefix: str, cache: Path | None = None) -> np.ndarray:
    """Unit vectors for `texts` with `prefix`, through the gate's embedder, cached by text."""
    from .systemone import normalise

    keys = [hashlib.sha256((prefix + t).encode()).hexdigest()[:24] for t in texts]
    known: dict[str, np.ndarray] = {}
    if cache is not None and cache.exists():
        try:
            with np.load(cache, allow_pickle=False) as data:
                known = dict(zip(data["keys"].tolist(), data["vectors"]))
        except (OSError, ValueError, KeyError):
            known = {}
    todo = [i for i, k in enumerate(keys) if k not in known]
    for start in range(0, len(todo), 256):
        chunk = todo[start:start + 256]
        vectors = normalise(await gate.embedder([prefix + texts[i] for i in chunk]))
        for i, v in zip(chunk, vectors):
            known[keys[i]] = v.astype(np.float32)
    if cache is not None and todo:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        with tmp.open("wb") as f:
            np.savez(f, keys=np.array(list(known)), vectors=np.stack(list(known.values())))
        tmp.replace(cache)
    return np.stack([known[k] for k in keys]).astype(np.float64)


async def evaluate(config, scorer: str, head: str = "", set_path: Path | None = None, name: str = ""):
    gate = await _gate(config, scorer=scorer, head=head)
    try:
        if scorer == "head" and gate.scorer != "head":
            raise GateCommandError(f"the head is not usable: {gate.head_fallback}")
        phrases, notifications = gateeval.load_set(set_path or gatehead.HELDOUT_SET)
        label = name or (f"head {gate.head.version}" if gate.scorer == "head" else "nearest")
        return await gateeval.run(gate, label, phrases, notifications)
    finally:
        await gate.close()


async def train(config, data: Path | None, activate: bool, output: Path | None, out=print) -> int:
    samples, dataset = gatehead.read_dataset(data or gatehead.TRAIN_SET)
    gate = await _gate(config, scorer="nearest")
    try:
        started = time.perf_counter()
        prefix = gate.config.query_prefix
        # Cached per backend too: Ollama's vectors are not the ONNX ones to the last digit (§8a).
        vectors = await embed(gate, [s.text for s in samples], prefix, cache_file(f"{gate.embedder_id}-{gate.backend}"))
        out(f"{len(samples)} sentences embedded in {time.perf_counter() - started:.1f}s ({gate.model_name})")
        started = time.perf_counter()
        head = gatehead.train(samples, vectors, _questions(), embedder=gate.embedder_id, query_prefix=prefix,
                              dataset=dataset, progress=out)
    finally:
        await gate.close()
    thresholds = head.meta["thresholds"]
    out(f"trained in {time.perf_counter() - started:.1f}s: act {head.act}, offer {head.offer} "
        f"(out of fold: {thresholds['fired']} reflex-eligible readings, {thresholds['fired_wrong']} wrong)")
    heads = gatehead.Heads()
    path = head.save(output) if output else heads.add(head)
    out(f"wrote {path}")
    results = [await evaluate(config, "nearest", name="nearest")]
    current, source, _ = gatehead.resolve(config.gate.head)
    if current is not None:
        try:
            results.append(await evaluate(config, "head", str(current.path), name=f"current ({source}) {current.version}"))
        except GateCommandError as exc:
            out(f"current head: {exc}")
    results.append(await evaluate(config, "head", str(path), name=f"new {head.version}"))
    out("\nheld-out set (" + str(gatehead.HELDOUT_SET) + "):\n")
    out(gateeval.table(results))
    if activate and not output:
        heads.use(path)
        out(f"\n{head.version} is now the current head ({heads.pointer}); restart the daemon to use it")
    elif not output:
        out(f"\nnot in use yet: strawberry gate use {head.version}")
    return 0


async def eval_command(config, scorer: str, head: str, set_path: Path | None, misses: bool, out=print) -> int:
    scorers = ("nearest", "head") if scorer == "both" else (scorer,)
    results = [await evaluate(config, s, head if s == "head" else "", set_path) for s in scorers]
    out(f"{set_path or gatehead.HELDOUT_SET}:\n")
    out(gateeval.table(results))
    if misses:
        for r in results:
            out(f"\n{r.name}: {len(r.misses)} sentence(s) with a miss")
            for text, wrong in r.misses:
                out(f"  {text!r}: {', '.join(wrong)}")
    return 0


def use(version: str, out=print) -> int:
    heads = gatehead.Heads()
    if version == "shipped":
        heads.use(None)
        out(f"the shipped head is in use ({gatehead.SHIPPED}); restart the daemon to use it")
        return 0
    path = heads.find(version)
    gatehead.load(path)                  # a broken file is refused here, not at the next start
    heads.use(path)
    out(f"{path.name} is in use ({heads.pointer}); restart the daemon to use it")
    return 0


def main(args, config) -> int:
    import asyncio

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.gate_command == "train":
            return asyncio.run(train(config, args.data, args.activate, args.output))
        if args.gate_command == "eval":
            return asyncio.run(eval_command(config, args.scorer, args.head or "", args.set, args.misses))
        return use(args.version)
    except (GateCommandError, gatehead.HeadError) as exc:
        print(f"strawberry gate: {exc}", file=sys.stderr)
        return 1
