"""CLI entrypoint: `uv run spacebrains [--port N] [--host H] [--log-level L]`."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from spacebrains.api.app import create_app
from spacebrains.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="spacebrains")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(
        app, host=args.host or cfg.host, port=args.port or cfg.port, log_level=args.log_level
    )


if __name__ == "__main__":
    main()
