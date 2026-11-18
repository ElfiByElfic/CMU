"""Malé trvalé úložiště v JSON souborech (uložené pohledy, historie změn).

Zápis je atomický (dočasný soubor + přejmenování). Bez `dataDir` drží data jen v paměti.
Pro jednu instanci backendu to stačí; pro více instancí by data musela jít do sdílené databáze.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class JsonStore:
    def __init__(self, directory: Path | None, name: str) -> None:
        self.path = directory / name if directory else None

    def load(self, default: Any) -> Any:
        if self.path is None or not self.path.exists():
            return default
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.error("nelze načíst %s: %s – začínám s prázdnými daty", self.path, e)
            return default

    def save(self, data: Any) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("nelze uložit %s: %s", self.path, e)
