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

    logging.basicConfig(
        level=config.daemon.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run(config)


if __name__ == "__main__":
    main()
