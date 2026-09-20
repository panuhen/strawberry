from __future__ import annotations

import argparse
import logging
import os

from . import __version__
from .server import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="strawberryd", description="Strawberry mascot daemon")
    parser.add_argument("--host", default=os.environ.get("STRAWBERRYD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STRAWBERRYD_PORT", "8770")))
    parser.add_argument("--log-level", default=os.environ.get("STRAWBERRYD_LOG", "INFO"))
    parser.add_argument("--version", action="version", version=f"strawberryd {__version__}")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.host, args.port)


if __name__ == "__main__":
    main()
