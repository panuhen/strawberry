from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, default_path, default_toml, load
from .server import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="strawberryd", description="Strawberry mascot daemon")
    parser.add_argument("--config", type=Path, default=None, help=f"settings file (default {default_path()})")
    parser.add_argument("--host", default=None, help="override daemon.host")
    parser.add_argument("--port", type=int, default=None, help="override daemon.port")
    parser.add_argument("--log-level", default=None, help="override daemon.log_level")
    parser.add_argument("--init-config", action="store_true", help="write a commented default config file and exit")
    parser.add_argument("--print-config", action="store_true", help="print the effective settings as JSON and exit")
    parser.add_argument("--print-port", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--say", metavar="TEXT", default=None, help="synthesise TEXT with the configured voice to a wav, print its path, exit")
    parser.add_argument("--voice", default=None, help="with --say: override speech.voice")
    parser.add_argument("--out", type=Path, default=None, help="with --say: wav path (default: a temp file)")
    parser.add_argument("--route", metavar="TEXT", default=None, help="run TEXT through the gate (WIRING §8a), print the JSON, exit")
    parser.add_argument("--tools", metavar="TOPIC", nargs="?", const="", default=None, help="list the MCP tools for TOPIC (or all), exit")
    parser.add_argument("--tool", metavar=("SERVER", "NAME"), nargs=2, default=None, help="call one MCP tool and print its result, exit")
    parser.add_argument("--args", metavar="JSON", default="{}", help="with --tool: the arguments as a JSON object")
    parser.add_argument("--think", metavar="TEXT", default=None, help="run TEXT through the thinker with the topic's tools (WIRING §8b), exit")
    parser.add_argument("--topic", default="music", help="with --think: which tools (default music)")
    parser.add_argument("--version", action="version", version=f"strawberryd {__version__}")
    args = parser.parse_args()

    path = args.config or default_path()
    if args.init_config:
        if path.exists():
            print(f"{path} already exists; not overwriting", file=sys.stderr)
            sys.exit(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_toml())
        print(path)
        return

    try:
        config = load(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    if args.host:
        config.daemon.host = args.host
    if args.port:
        config.daemon.port = args.port
    if args.log_level:
        config.daemon.log_level = args.log_level

    if args.print_port:
        print(config.daemon.port)
        return
    if args.print_config:
        print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
        return
    if args.say is not None:
        sys.exit(say(config, args.say, args.voice, args.out))
    if args.route is not None:
        sys.exit(route(config, args.route))
    if args.tools is not None or args.tool is not None:
        sys.exit(tools(config, args.tools, args.tool, args.args))
    if args.think is not None:
        sys.exit(think(config, args.think, args.topic))

    logging.basicConfig(
        level=config.daemon.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run(config)


def route(config, text: str) -> int:
    """`strawberryd --route "..."`: the gate's reading of one sentence, for tuning the examples."""
    import asyncio
    from dataclasses import replace

    from .systemone import Gate

    gate = Gate(replace(config.gate, enabled=True), config.brain.ollama_url)

    async def run_once() -> int:
        await gate.start()
        try:
            if not gate.ready:
                print(f"gate not ready: {gate.disabled_reason}", file=sys.stderr)
                return 1
            result = await gate.route(text)
            if result is None:
                return 1
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            return 0
        finally:
            await gate.close()

    return asyncio.run(run_once())


def tools(config, topic: str | None, call: list[str] | None, arguments: str) -> int:
    """`strawberryd --tools [TOPIC]` / `--tool SERVER NAME --args JSON`: the MCP servers by hand."""
    import asyncio
    from dataclasses import replace

    from .tools import Toolbox

    try:
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError
    except ValueError:
        print("--args must be a JSON object", file=sys.stderr)
        return 2
    toolbox = Toolbox(replace(config.tools, enabled=True, preconnect=False))

    async def run_once() -> int:
        try:
            if call:
                result = await toolbox.call(call[0], call[1], parsed)
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
                return 0 if result.ok else 1
            topics = [topic] if topic else sorted(toolbox.topics())
            for name in topics:
                specs = await toolbox.tools_for(name)
                print(f"[{name}] {len(specs)} tools")
                for spec in specs:
                    required = spec.schema.get("required", [])
                    params = ", ".join(f"{p}{'' if p in required else '?'}" for p in spec.schema.get("properties", {}))
                    print(f"  {spec.server}.{spec.name}({params})  {spec.description.splitlines()[0][:80] if spec.description else ''}")
            for name, stats in toolbox.stats().items():
                if stats["error"]:
                    print(f"  {name}: {stats['error']}", file=sys.stderr)
            return 0
        finally:
            await toolbox.close()

    return asyncio.run(run_once())


def think(config, text: str, topic: str) -> int:
    """`strawberryd --think "..."`: the thinker by hand, printing the fact, the tool calls and the time."""
    import asyncio
    from dataclasses import replace

    from .actions import Actor
    from .thinker import Thinker
    from .tools import Toolbox

    logging.basicConfig(level="INFO", format="%(levelname)-7s %(name)s: %(message)s")
    toolbox = Toolbox(replace(config.tools, enabled=True, preconnect=False))
    thinker = Thinker(replace(config.thinker, enabled=True), toolbox, config.brain.action_model, config.brain.ollama_url)
    actor = Actor(config.actions, toolbox)

    async def run_once() -> int:
        await thinker.start()
        try:
            situation = await actor.situation(topic)
            names = await actor.vocabulary()
            if names:
                situation = f"{situation} Names in the user's library: {', '.join(names[: config.voice.max_hotwords])}.".strip()
            print(f"situation: {situation or '(none)'}", file=sys.stderr)
            outcome = await thinker.run(text, topic, situation)
            print(json.dumps(thinker.last, indent=2, ensure_ascii=False))
            return 0 if outcome.ok else 1
        finally:
            await thinker.close()
            await toolbox.close()

    return asyncio.run(run_once())


def say(config, text: str, voice: str | None, out: Path | None) -> int:
    """`strawberryd --say "..."`: one wav with the configured (or given) voice, for auditioning."""
    import asyncio
    import shutil
    import tempfile
    from dataclasses import replace

    from .speech import Speaker, wav_seconds

    speech = replace(config.speech, enabled=True, quiet_hours="", voice=voice or config.speech.voice)
    speaker = Speaker(speech)

    async def run_once() -> int:
        await speaker.start()
        if not speaker.ready:
            print(f"cannot speak: {speaker.disabled_reason}", file=sys.stderr)
            return 3
        path = await speaker.say(text)
        if not path:
            print("nothing to say", file=sys.stderr)
            return 3
        target = out or Path(tempfile.gettempdir()) / f"strawberry-{Path(speech.voice).stem}.wav"
        shutil.copyfile(path, target)
        await speaker.close()
        print(f"{target}\t{wav_seconds(target):.2f}s\t{speaker.last_ms:.0f}ms", file=sys.stderr)
        print(target)
        return 0

    return asyncio.run(run_once())


if __name__ == "__main__":
    main()
