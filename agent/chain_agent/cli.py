"""Spuštění: chain-agent --config /etc/chain-agent/agent.json"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from . import __version__
from .api import create_app
from .config import AgentConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chain-agent", description="Správa řetězců aplikací na VM")
    parser.add_argument("--config", default="/etc/chain-agent/agent.json", help="konfigurační soubor agenta")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = AgentConfig.load(args.config)
    except Exception as e:
        logging.critical("nelze načíst konfiguraci %s: %s", args.config, e)
        return 1

    uvicorn.run(create_app(cfg), host=cfg.listenAddress, port=cfg.listenPort,
                log_level=args.log_level, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
