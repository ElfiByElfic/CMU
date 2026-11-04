"""Spuštění: cmu-backend --config /etc/cmu/cmu.yaml"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from . import __version__
from .api import create_app
from .settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cmu-backend", description="CMU – centrální backend")
    parser.add_argument("--config", default="/etc/cmu/cmu.yaml", help="konfigurační soubor (YAML)")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.load(args.config)
    except Exception as e:
        logging.critical("nelze načíst konfiguraci %s: %s", args.config, e)
        return 1
    uvicorn.run(create_app(settings), host=settings.listenAddress, port=settings.listenPort,
                log_level=args.log_level, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
