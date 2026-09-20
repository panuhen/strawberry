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

    logging.basicConfig(
        level=config.daemon.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run(config)


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
