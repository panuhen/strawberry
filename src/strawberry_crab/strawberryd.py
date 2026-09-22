"""`strawberryd`: the daemon, and the by-hand tools that share its config (--route, --say, ...).

The `strawberry` CLI (cli.py) calls main() for its route/tools/tool/think/talk/tray commands, and
the tray runs it as `python -m strawberry_crab.strawberryd` for the daemon child.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__, osguard
from .config import ConfigError, default_path, default_toml, load
from .server import run


def main(argv: list[str] | None = None) -> None:
    osguard.require_supported()
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
    parser.add_argument("--think", metavar="TEXT", default=None, help="run TEXT through the thinker (Qwen + the tools, WIRING §8b), exit")
    parser.add_argument("--talk", action="store_true", help="type to her: each line goes through the running daemon as if spoken; shows the routing")
    parser.add_argument("--tray", action="store_true", help="run the tray icon: the login process that starts everything else (WIRING §14)")
    parser.add_argument("--no-children", action="store_true", help="with --tray: just the icon, against a daemon that is already up")
    parser.add_argument("--no-widget", action="store_true", help="with --tray: start the daemon and doorways but not the widget")
    parser.add_argument("--version", action="version", version=f"strawberryd {__version__}")
    args = parser.parse_args(argv)

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
        sys.exit(think(config, args.think))
    if args.talk:
        sys.exit(talk(config))
    if args.tray:
        sys.exit(tray(config, children=not args.no_children, widget=not args.no_widget, config_path=args.config))

    logging.basicConfig(
        level=config.daemon.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run(config)


def tray(config, children: bool = True, widget: bool = True, config_path: Path | None = None) -> int:
    """`strawberryd --tray`: the 🍓 in the top bar, and (unless --no-children) everything under it."""
    import asyncio

    from .tray import run_tray

    logging.basicConfig(level=config.daemon.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        return asyncio.run(run_tray(config.daemon.port, children=children, widget=widget, config=config_path))
    except KeyboardInterrupt:
        return 0


def route(config, text: str) -> int:
    """`strawberryd --route "..."`: the gate's reading of one sentence, for tuning the examples."""
    import asyncio
    from dataclasses import replace

    from .adapters import gate_examples, load
    from .systemone import Gate

    # The same questions the daemon asks, adapter phrases included, or the reading would differ.
    gate = Gate(replace(config.gate, enabled=True), config.brain.ollama_url,
                examples=gate_examples(load(config.tools.servers) if config.tools.enabled else {}))

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


def talk(config) -> int:
    """`strawberryd --talk`: a terminal mouth. Each line is posted to the running daemon as a voice
    event, so the gate, the reflexes and the thinker treat it exactly like a spoken sentence and
    she answers on the desktop as well as here. After each reply the routing is shown."""
    import urllib.error
    import urllib.request

    base = f"http://{config.daemon.host}:{config.daemon.port}"

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return json.loads(response.read())

    def post(path: str, body: dict):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())

    try:
        health = get("/health")
    except (urllib.error.URLError, OSError) as exc:
        print(f"strawberryd is not answering at {base} ({exc}); start it with: strawberry daemon", file=sys.stderr)
        return 1
    print(f"talking to strawberryd at {base}; gate {'ready' if health['gate']['ready'] else 'off'}, "
          f"thinker {health['thinker']['model'] or 'off'}. Empty line or Ctrl-D to leave.")
    while True:
        try:
            line = input("you: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            return 0
        try:
            reply = post("/event", {"source": "voice", "title": line})
            health = get("/health")
        except (urllib.error.URLError, OSError) as exc:
            print(f"  (daemon error: {exc})", file=sys.stderr)
            continue
        print(f"she: {reply['performance'].get('text', '')}")
        route = health["gate"].get("last_route") or {}
        if route.get("text") == line:
            tool = f" tool {route['tool']} {route['tool_confidence']:.2f}" if route.get("tool") else ""
            print(f"     [{route['kind']}/{route['topic']} {route['confidence']:.2f} -> {route['decision']}{tool} arg {route['has_argument']:.2f}]")
        action = health["actions"].get("last") or {}
        if action.get("asked") == line:
            calls = ", ".join(f"{c['server']}.{c['name']}" for c in action.get("calls", []))
            print(f"     [reflex {action['tool']}: {calls} in {action['ms']:.0f} ms]")
        thought = health["thinker"].get("last") or {}
        if thought.get("asked") == line:
            calls = ", ".join(f"{c['server']}.{c['name']}" for c in thought.get("calls", [])) or "no tools"
            print(f"     [thinker [{thought['emotion']}]: {calls} in {thought['s']:.1f} s]")


def think(config, text: str) -> int:
    """`strawberryd --think "..."`: the thinker by hand, printing her line, the tool calls and the time."""
    import asyncio
    from dataclasses import replace

    from .actions import Actor
    from .mpris import Mpris
    from .thinker import Thinker
    from .tools import Toolbox

    logging.basicConfig(level="INFO", format="%(levelname)-7s %(name)s: %(message)s")
    toolbox = Toolbox(replace(config.tools, enabled=True, preconnect=False))
    thinker = Thinker(replace(config.thinker, enabled=True), toolbox, config.brain.action_model, config.brain.ollama_url)
    mpris = Mpris() if config.actions.mpris else None
    actor = Actor(config.actions, toolbox, mpris=mpris)

    async def run_once() -> int:
        await thinker.start()
        try:
            import time as _time

            situation = f"{_time.strftime('Today is %A %d %B %Y, %H:%M local time.')} {await actor.situation()}".strip()
            names = await actor.vocabulary()
            if names:
                situation = f"{situation} Names in the user's library: {', '.join(names[: config.voice.max_hotwords])}.".strip()
            print(f"situation: {situation or '(none)'}", file=sys.stderr)
            outcome = await thinker.run(text, situation)
            print(json.dumps(thinker.last, indent=2, ensure_ascii=False))
            return 0 if outcome.ok else 1
        finally:
            await thinker.close()
            await toolbox.close()
            if mpris:
                await mpris.close()

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
